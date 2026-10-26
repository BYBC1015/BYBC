from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from .codex_exec import CodexExecResult, ProcessRunner, first_non_empty
from .subprocess_utils import hidden_subprocess_kwargs


class HermesExecRunner:
    def __init__(self, config: Any, process_runner: ProcessRunner | None = None):
        self.config = config
        self.process_runner = process_runner or subprocess

    def run(
        self,
        project_slug: str,
        project_path: Path,
        prompt_path: Path,
        prompt: str,
        execution_preferences: dict[str, str] | None = None,
    ) -> CodexExecResult:
        cli_path = getattr(self.config, "hermes_cli_path", None)
        output_path = self._output_path(project_slug, prompt_path)
        if not cli_path:
            return self._failed(
                command=[],
                output_path=output_path,
                error="Hermes CLI path is not configured.",
            )

        cli_path = Path(cli_path).expanduser()
        if not cli_path.exists():
            return self._failed(
                command=[str(cli_path)],
                output_path=output_path,
                error=f"Hermes CLI not found: {cli_path}",
            )

        project_path = project_path.expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        timeout = max(30, int(getattr(self.config, "hermes_timeout_seconds", 1800)))
        hermes_prompt = self._build_prompt(prompt_path, prompt)
        command = self._build_command(
            cli_path, hermes_prompt, execution_preferences=execution_preferences
        )

        try:
            completed = self.process_runner.run(
                command,
                cwd=str(project_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                **hidden_subprocess_kwargs(),
            )
        except subprocess.TimeoutExpired as exc:
            stdout = coerce_text(exc.stdout)
            stderr = coerce_text(exc.stderr)
            last_message = first_non_empty(stdout, stderr)
            write_text(output_path, last_message)
            return CodexExecResult(
                ok=False,
                command=command,
                returncode=None,
                stdout=stdout,
                stderr=stderr,
                last_message=last_message,
                output_path=output_path,
                error=f"Hermes exec timed out after {timeout} seconds.",
            )
        except OSError as exc:
            return self._failed(
                command=command,
                output_path=output_path,
                error=f"Hermes exec could not start: {exc}",
            )

        last_message = first_non_empty(
            completed.stdout or "",
            completed.stderr or "",
            "Hermes completed.",
        )
        write_text(output_path, last_message)
        return CodexExecResult(
            ok=completed.returncode == 0,
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            last_message=last_message,
            output_path=output_path,
            error=""
            if completed.returncode == 0
            else f"Hermes exec exited with code {completed.returncode}.",
        )

    def _build_command(
        self,
        cli_path: Path,
        prompt: str,
        execution_preferences: dict[str, str] | None = None,
    ) -> list[str]:
        command = [str(cli_path)]
        preferences = execution_preferences or {}
        model = str(
            preferences.get("model")
            or getattr(self.config, "hermes_model", "")
            or ""
        ).strip()
        reasoning_effort = str(
            preferences.get("reasoning_effort") or ""
        ).strip().lower()
        provider = str(getattr(self.config, "hermes_provider", "") or "").strip()
        toolsets = str(getattr(self.config, "hermes_toolsets", "") or "").strip()
        if model:
            command.extend(["--model", model])
        if reasoning_effort:
            command.extend(["--reasoning", reasoning_effort])
        if provider:
            command.extend(["--provider", provider])
        if toolsets:
            command.extend(["--toolsets", toolsets])
        command.extend(["--oneshot", prompt])
        return command

    def _build_prompt(self, prompt_path: Path, prompt: str) -> str:
        return "\n".join(
            [
                "# Hermes Hub Assistant",
                "",
                "You are a local Hermes advisory assistant connected to Codex Hub.",
                "Only respond to this Hub item. Keep Codex as the primary decision maker.",
                "Do not touch SW /upload or /u unless the Hub item explicitly says to do so.",
                "Do not modify files, services, DNS, deployments, or secrets unless the Hub item explicitly asks Hermes to perform that action.",
                "Return concise Markdown. If the prompt names a primary device, include that @mention in your final answer.",
                "",
                "## Hub Item",
                prompt,
                "",
                "## Local Prompt File",
                f"Prompt file: {prompt_path}",
            ]
        )

    def _output_path(self, project_slug: str, prompt_path: Path) -> Path:
        output_dir = Path(getattr(self.config, "hermes_output_dir"))
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


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((value or "").strip(), encoding="utf-8")


def coerce_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
