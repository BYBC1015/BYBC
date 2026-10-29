from __future__ import annotations

import subprocess
import time
import json
import re
import os
import signal
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .codex_stream import ProgressCallback, ProgressDispatcher, VisibleCodexProgress
from .subprocess_utils import hidden_subprocess_kwargs
from .tool_locator import resolve_codex_cli_path
from .token_usage import normalize_token_usage, usage_from_jsonl


class ProcessRunner(Protocol):
    def run(self, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]: ...


@dataclass
class CodexExecResult:
    ok: bool
    command: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    last_message: str
    output_path: Path
    error: str = ""
    token_usage: dict[str, int] | None = None

    @property
    def summary(self) -> str:
        if self.ok:
            return first_non_empty(self.last_message, self.stdout, "Codex exec completed.")
        return first_non_empty(self.error, self.stderr, self.stdout, "Codex exec failed.")


class CodexExecRunner:
    def __init__(self, config: Any, process_runner: ProcessRunner | None = None):
        self.config = config
        self.process_runner = process_runner or subprocess

    def run(
        self, project_slug: str, project_path: Path, prompt_path: Path, prompt: str,
        execution_preferences: dict[str, str] | None = None,
        image_paths: list[Path] | None = None,
        execution_context: dict | None = None,
        cancel_event: threading.Event | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> CodexExecResult:
        context = execution_context or {}
        continuous_chat = (context.get('execution_route') == 'chat'
                           and context.get('assigned_agent_kind') == 'controller'
                           and context.get('conversation_id')
                           and not context.get('controller_read_only'))
        if context.get('execution_route') == 'project' or continuous_chat:
            result = self._run_project(project_slug, project_path, prompt_path, prompt,
                                       execution_preferences, image_paths, execution_context, cancel_event, progress_callback)
        else:
            result = self._run(project_slug, project_path, prompt_path, prompt,
                               execution_preferences, image_paths, execution_context, cancel_event,
                               progress_callback=progress_callback)
        if execution_context:
            # Persist the actual process outcome before returning to the worker.
            # A last-message file alone is not evidence that the process exited.
            receipt_path = Path(execution_context['receipt_path'])
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {key: execution_context[key] for key in ('generation', 'run_key')}
            message = self._redact(result.last_message)
            if len(message) > 100000:
                message = message[:80000] + '\n[中段较长，完整结果见输出文件]\n' + message[-20000:]
            payload.update(ok=result.ok, returncode=result.returncode,
                           last_message=message,
                           error=self._redact(result.error)[:4000], output_path=str(result.output_path))
            usage = normalize_token_usage(result.token_usage)
            if usage is not None:payload['token_usage'] = usage
            temporary = receipt_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            temporary.replace(receipt_path)
        return result

    def _run_project(self, project_slug, project_path, prompt_path, prompt,
                     execution_preferences, image_paths, execution_context, cancel_event, progress_callback=None):
        from .project_sessions import ProjectSessionStore, ProjectSessionError

        options = getattr(self.config, 'backend_options', {}) or {}
        root = options.get('project_session_root') or self.config.state_path.parent / 'project-sessions'
        result = None
        try:
            with ProjectSessionStore(root).acquire(
                device_id=execution_context.get('device_id'), project_slug=project_slug,
                conversation_id=execution_context.get('conversation_id'),
                assigned_agent_id=execution_context.get('assigned_agent_id'), project_path=project_path,
                run_key=execution_context['run_key'], generation=execution_context['generation'],
            ) as session:
                if session.cached_receipt:
                    cached = session.cached_receipt
                    return CodexExecResult(ok=cached['ok'], command=[], returncode=cached['returncode'],
                        stdout='', stderr='', last_message=cached.get('last_message') or '',
                        output_path=Path(cached.get('output_path') or self._output_path(project_slug, prompt_path)),
                        error=cached.get('error') or '', token_usage=normalize_token_usage(cached.get('token_usage')))
                result = self._run(project_slug, project_path, prompt_path, prompt,
                    execution_preferences, image_paths, execution_context, cancel_event,
                    project_session=session, progress_callback=progress_callback)
                if session.started:
                    output = result.stdout.decode('utf-8', errors='replace') if isinstance(result.stdout, bytes) else result.stdout
                    session.observe_jsonl(output)
                    session.complete(ok=result.ok, returncode=result.returncode,
                        last_message=self._redact(result.last_message)[:262144],
                        error=self._redact(result.error)[:65536], output_path=result.output_path,
                        token_usage=result.token_usage)
                return result
        except ProjectSessionError as exc:
            label = '总控对话' if execution_context.get('execution_route') == 'chat' else '本地工程会话'
            if result is not None:
                # A timeout/process failure explains why the receipt remains
                # uncertain. Do not replace that evidence with a generic lock
                # message or claim that no actual work took place.
                detail = '\n'.join(part for part in (result.error, label + '暂未继续：' + str(exc)) if part)
                return CodexExecResult(ok=False, command=result.command, returncode=result.returncode,
                    stdout=result.stdout, stderr=result.stderr, last_message='', output_path=result.output_path,
                    error=detail, token_usage=result.token_usage)
            return self._failed(command=[], output_path=self._output_path(project_slug, prompt_path),
                                error=label + '暂未继续：' + str(exc))

    def _run(
        self,
        project_slug: str,
        project_path: Path,
        prompt_path: Path,
        prompt: str,
        execution_preferences: dict[str, str] | None = None,
        image_paths: list[Path] | None = None,
        execution_context: dict | None = None,
        cancel_event: threading.Event | None = None,
        project_session=None,
        progress_callback: ProgressCallback | None = None,
    ) -> CodexExecResult:
        cli_path = getattr(self.config, "codex_cli_path", None)
        if not cli_path:
            return self._failed(
                command=[],
                output_path=self._output_path(project_slug, prompt_path),
                error="Codex CLI path is not configured.",
            )

        configured_cli_path = Path(cli_path).expanduser()
        cli_path = resolve_codex_cli_path(configured_cli_path)
        if cli_path is None:
            return self._failed(
                command=[str(configured_cli_path)],
                output_path=self._output_path(project_slug, prompt_path),
                error=f"Codex CLI not found: {configured_cli_path}",
            )

        project_path = project_path.expanduser().resolve()
        output_path = self._output_path(project_slug, prompt_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        timeout = max(30, int(getattr(self.config, "codex_exec_timeout_seconds", 1800)))
        sandbox = str(getattr(self.config, "codex_exec_sandbox", "workspace-write") or "workspace-write")
        command = [
            str(cli_path),
            "--ask-for-approval",
            "never",
        ]
        preferences = execution_preferences or {}
        options = getattr(self.config, "backend_options", {})
        options = options if isinstance(options, dict) else {}
        model = str(preferences.get("model") or options.get("codex_model") or "").strip()
        if model and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}", model):
            return self._failed(command=[], output_path=output_path, error="Configured Codex model identifier is invalid.")
        reasoning_effort = str(
            preferences.get("reasoning_effort") or ""
        ).strip().lower()
        if model:
            command.extend(["--model", model])
        if reasoning_effort:
            command.extend(
                ["-c", f'model_reasoning_effort="{reasoning_effort}"']
            )
        windows_sandbox = options.get("windows_sandbox")
        if windows_sandbox:
            if windows_sandbox not in ("elevated", "unelevated", "mxc"):
                return self._failed(command=[], output_path=output_path, error="Configured Windows sandbox is invalid.")
            command.extend(["-c", f'windows.sandbox="{windows_sandbox}"'])
        if (sandbox == "workspace-write" and isinstance(options, dict)
                and options.get("agent_network_access") is True):
            # Explicit per-runtime opt-in for the dedicated agent worker's Hub
            # delegation helper. Keep the filesystem sandbox and old bindings.
            command.extend(["-c", "sandbox_workspace_write.network_access=true"])
        command.append("exec")
        if project_session:
            # Parent exec options retain the exact workspace and sandbox on resume.
            command.extend(['--cd', str(project_path), '--sandbox', sandbox])
            if project_session.session_id:
                command.append('resume')
            command.append('--json')
        elif execution_context or progress_callback:
            command.append('--json')
        if bool(getattr(self.config, "codex_exec_ignore_user_config", True)):
            command.append("--ignore-user-config")
        for image_path in image_paths or []:
            resolved = Path(image_path).resolve()
            if project_path not in resolved.parents or not resolved.is_file():
                return self._failed(command=[], output_path=output_path,
                                    error="Task image is missing or outside the configured project.")
            command.extend(["--image", str(resolved)])
        if not project_session:
            command.extend(['--cd', str(project_path), '--sandbox', sandbox])
        command.extend(['--skip-git-repo-check', '--output-last-message', str(output_path)])
        if project_session and project_session.session_id:
            command.append(project_session.session_id)
        command.append('-')

        process_options = hidden_subprocess_kwargs()
        process_options['cwd'] = str(project_path)
        if execution_context:
            child_environment = os.environ.copy()
            for field, variable in [('generation', 'GENERATION'), ('run_key', 'RUN_KEY'),
                                    ('task_id', 'TASK_ID'), ('project_slug', 'PROJECT')]:
                child_environment['CODEX_HUB_EXECUTION_' + variable] = str(execution_context[field])
            process_options['env'] = child_environment
        try:
            if cancel_event is not None and cancel_event.is_set():
                return self._failed(command=command, output_path=output_path,
                                    error='Hub 已撤销本次执行许可，工作进程未启动。')
            if project_session:
                project_session.start()
            if self.process_runner is subprocess and (cancel_event is not None or project_session or progress_callback):
                completed = self._run_cancellable(command, prompt, timeout, process_options,
                    cancel_event if cancel_event is not None else threading.Event(),
                    on_unstarted=project_session.abort_unstarted if project_session else None,
                    progress_callback=progress_callback,
                    event_callback=project_session.observe_jsonl if project_session else None)
            else:
                completed = self.process_runner.run(
                    command, input=prompt, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=timeout, check=False,
                    **process_options,
                )
        except subprocess.TimeoutExpired as exc:
            detail = f"Codex exec timed out after {timeout} seconds."
            self._write_diagnostic(output_path, detail, exc.stderr)
            return CodexExecResult(
                ok=False,
                command=command,
                returncode=None,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                last_message=read_text_if_exists(output_path),
                output_path=output_path,
                error=detail,
            )
        except OSError as exc:
            return self._failed(
                command=command,
                output_path=output_path,
                error=f"Codex exec could not start: {exc}",
            )

        last_message = read_text_if_exists(output_path)
        error = ""
        if completed.returncode != 0:
            detail = self._safe_error_detail(completed.stderr or "")
            error = f"Codex exec exited with code {completed.returncode}." + (f" {detail}" if detail else "")
            self._write_diagnostic(output_path, error, completed.stderr)
        elif "blocked by policy" in (completed.stderr or "").casefold():
            self._write_diagnostic(output_path, "A command was blocked by policy; inspect the task reflection.", completed.stderr)
        return CodexExecResult(
            ok=completed.returncode == 0,
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            last_message=last_message,
            output_path=output_path,
            error=error,
            token_usage=usage_from_jsonl(completed.stdout),
        )

    @staticmethod
    def _stop_owned_process_tree(process: subprocess.Popen) -> bool:
        # Only the PID launched for this leased execution is a target. Never
        # enumerate/stop unrelated Codex, Bridge, or desktop processes.
        tree_stopped = False
        if os.name == 'nt':
            taskkill = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'taskkill.exe'
            try:
                stopped = subprocess.run([str(taskkill), '/PID', str(process.pid), '/T', '/F'],
                    capture_output=True, timeout=12, check=False, **hidden_subprocess_kwargs())
                tree_stopped = stopped.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                tree_stopped = True
            except ProcessLookupError:
                tree_stopped = True
        if process.poll() is None:
            process.kill()
        return tree_stopped

    def _run_cancellable(self, command, prompt, timeout, process_options, cancel_event,
                         on_unstarted=None, progress_callback=None, event_callback=None):
        if progress_callback is not None or event_callback is not None:
            return self._run_streaming(command, prompt, timeout, process_options, cancel_event,
                                       on_unstarted, progress_callback or (lambda *_: None), event_callback)
        if cancel_event.is_set():
            if on_unstarted:
                on_unstarted()
            return subprocess.CompletedProcess(command, -1, '', 'ERROR: Hub 已撤销本次执行许可，工作进程未启动。')
        options = dict(process_options)
        if os.name != 'nt':
            options['start_new_session'] = True
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding='utf-8', errors='replace', **options)
        except OSError:
            if on_unstarted:
                on_unstarted()
            raise
        with process:
            deadline = time.monotonic() + timeout
            first_input = prompt
            while True:
                cancelled = cancel_event.is_set()
                expired = time.monotonic() >= deadline
                if cancelled or expired:
                    tree_stopped = self._stop_owned_process_tree(process)
                    stdout, stderr = process.communicate(timeout=10)
                    if expired and not cancelled:
                        raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
                    detail = ('本次 CLI 及其子进程已停止。' if tree_stopped else
                              '本次 CLI 已停止，子进程状态需核对；不会重跑任务。')
                    return subprocess.CompletedProcess(command, process.returncode or -1, stdout,
                        (stderr or '') + '\nERROR: Hub 已明确撤销本次执行许可，' + detail)
                try:
                    stdout, stderr = process.communicate(input=first_input,
                        timeout=min(1.0, max(.01, deadline - time.monotonic())))
                    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    # communicate retains its input/output buffers across timeouts;
                    # retry without resending stdin and without blocking on reads.
                    first_input = None

    def _run_streaming(self, command, prompt, timeout, process_options, cancel_event,
                       on_unstarted, progress_callback, event_callback=None):
        if cancel_event.is_set():
            if on_unstarted:
                on_unstarted()
            return subprocess.CompletedProcess(command, -1, '', 'ERROR: Hub 已撤销本次执行许可，工作进程未启动。')
        options = dict(process_options)
        if os.name != 'nt':
            options['start_new_session'] = True
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding='utf-8', errors='replace', **options)
        except OSError:
            if on_unstarted:
                on_unstarted()
            raise

        dispatcher = ProgressDispatcher(progress_callback)
        progress = VisibleCodexProgress(dispatcher.publish, self._redact)
        stdout_parts, stderr_parts = [], []
        event_errors = []

        def read_stdout():
            try:
                for line in process.stdout:
                    stdout_parts.append(line)
                    if event_callback is not None and not event_errors:
                        try:
                            event_callback(line)
                        except Exception as exc:
                            event_errors.append(exc)
                    try:
                        progress.feed(line)
                    except Exception:
                        # Malformed/unknown progress must not lose the raw JSONL
                        # needed to validate the project session receipt later.
                        pass
            finally:
                process.stdout.close()

        def read_stderr():
            try:
                while True:
                    chunk = process.stderr.read(65536)
                    if not chunk:
                        return
                    stderr_parts.append(chunk)
            finally:
                process.stderr.close()

        def write_prompt():
            try:
                process.stdin.write(prompt)
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass

        readers = [threading.Thread(target=read_stdout, name='hub-cli-stdout', daemon=True),
                   threading.Thread(target=read_stderr, name='hub-cli-stderr', daemon=True)]
        writer = threading.Thread(target=write_prompt, name='hub-cli-stdin', daemon=True)
        for thread in readers:
            thread.start()
        writer.start()
        progress.started()
        deadline = time.monotonic() + timeout
        cancelled = expired = tree_stopped = False
        try:
            while process.poll() is None or any(thread.is_alive() for thread in readers):
                cancelled = cancel_event.is_set()
                expired = time.monotonic() >= deadline
                if cancelled or expired:
                    tree_stopped = self._stop_owned_process_tree(process)
                    break
                cancel_event.wait(min(.05, max(.001, deadline - time.monotonic())))
            process.wait(timeout=10)
            for thread in [writer, *readers]:
                thread.join(timeout=2)
            stdout, stderr = ''.join(stdout_parts), ''.join(stderr_parts)
            if event_errors:
                raise event_errors[0]
            if expired and not cancelled:
                raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
            if cancelled:
                detail = ('本次 CLI 及其子进程已停止。' if tree_stopped else
                          '本次 CLI 已停止，子进程状态需核对；不会重跑任务。')
                stderr += '\nERROR: Hub 已明确撤销本次执行许可，' + detail
                return subprocess.CompletedProcess(command, process.returncode or -1, stdout, stderr)
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        finally:
            dispatcher.close()

    def _redact(self, value: str | bytes | None) -> str:
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        text = value or ""
        token = str(getattr(self.config, "token", "") or "")
        if token:
            text = text.replace(token, "[REDACTED]")
        text = re.sub(r'(?i)(authorization\s*[:=]\s*["\']?bearer\s+)[^\s"\']+', r'\1[REDACTED]', text)
        text = re.sub(r'(?i)(["\']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|client_secret|secret)["\']?\s*[:=]\s*["\']?)[^\s,"\'}]+', r'\1[REDACTED]', text)
        return re.sub(r'\b(?:sk-[A-Za-z0-9_-]{10,}|eyJ[A-Za-z0-9_-]{20,}(?:\.[A-Za-z0-9_-]+){1,2})\b', '[REDACTED]', text)

    def _safe_error_detail(self, stderr: str) -> str:
        # CLI stderr also echoes the task prompt. Only surface final error lines,
        # rather than copying private task context or startup warnings into Hub.
        lines = [line.strip()[6:].strip() for line in stderr.splitlines() if line.strip().upper().startswith("ERROR:")]
        detail = " / ".join(dict.fromkeys(lines[-3:]))
        return self._redact(detail)[:1400]

    def _write_diagnostic(self, output_path: Path, error: str, stderr: str | bytes | None) -> None:
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            diagnostic = output_path.with_suffix(".diagnostic.json")
            diagnostic.write_text(json.dumps({"error": self._redact(error),
                "stderr_tail": self._redact(stderr)[-12000:]}, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            # Diagnostic persistence must never hide the original process error.
            pass

    def _output_path(self, project_slug: str, prompt_path: Path) -> Path:
        output_dir = Path(getattr(self.config, "codex_exec_output_dir"))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return output_dir / project_slug / f"{prompt_path.stem}-{stamp}-last-message.md"

    @staticmethod
    def _failed(command: list[str], output_path: Path, error: str) -> CodexExecResult:
        return CodexExecResult(
            ok=False,
            command=command,
            returncode=None,
            stdout="",
            stderr="",
            last_message="",
            output_path=output_path,
            error=error,
        )


def read_text_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()


def first_non_empty(*values: str) -> str:
    for value in values:
        if value and value.strip():
            return value.strip()
    return ""
