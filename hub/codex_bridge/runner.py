from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlencode

from .client import HubClient
from .agent_runtime import CodexAgentDecisionRunner, extract_reflection, ExecutionRenewal, read_execution_completion
from .codex_app_submitter import CodexAppSubmitter
from .codex_exec import CodexExecResult, CodexExecRunner
from .attachments import PreparedAttachments, AttachmentPreparationError, prepare_task_attachments
from .config import BridgeConfig
from .hermes_exec import HermesExecRunner
from .launcher import CodexLauncher
from .model_catalog import CodexModelCatalogProvider
from .state import BridgeState
from codex_hub.text_safety import validate_project_memory_document

MAX_INLINE_CODEX_PROMPT_CHARS = 6000
MAX_ENCODED_CODEX_PROMPT_CHARS = 6000
MAX_HUB_RESULT_CHARS = 12000
MAX_MANIFEST_FILES = 10_000
MAX_AUTOMATIC_HASH_BYTES = 64 * 1024 * 1024
MEMORY_VERSION_RE = re.compile(r"^Memory version:\s*(\d+)\s*$", re.MULTILINE)
MANIFEST_IGNORED_DIRS = {
    ".git",
    ".gradle",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "node_modules",
}
MANIFEST_IGNORED_FILES = {".env", "config.json"}
PROJECT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")
REASONING_EFFORT_VALUES = {
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
}
CODEX_TARGET_PROJECT_RE = re.compile(
    r"Codex target project slug:\s*([a-z0-9][a-z0-9-]{1,62}[a-z0-9])",
    re.IGNORECASE,
)
PROJECT_THREAD_REQUEST_RE = re.compile(
    r"在\s*([A-Za-z0-9_.\-\u4e00-\u9fff]+)\s*项目\s*(?:中|里)?\s*新建(?:一个)?(?:\s*Codex)?(?:对话|会话|线程)",
    re.IGNORECASE,
)
THREAD_PROMPT_RE = re.compile(
    r"(?:内容|prompt|提示)(?:为|是|:|：)\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)


class HubClientProtocol(Protocol):
    def get_inbox(self) -> dict[str, Any]: ...

    def claim_task(self, project_slug: str, task_id: int) -> dict[str, Any] | None: ...

    def get_task_prompt(self, project_slug: str, task_id: int) -> str: ...

    def claim_mention(
        self, project_slug: str, entry_id: int
    ) -> dict[str, Any] | None: ...

    def mark_mention_handled(self, project_slug: str, entry_id: int) -> None: ...

    def mark_mention_dispatching(
        self, project_slug: str, entry_id: int
    ) -> dict[str, Any]: ...

    def mark_mention_submitted(
        self, project_slug: str, entry_id: int
    ) -> dict[str, Any]: ...

    def release_mention_dispatch(
        self, project_slug: str, entry_id: int
    ) -> dict[str, Any]: ...

    def update_task_status(
        self,
        project_slug: str,
        task_id: int,
        status: str,
        result_summary: str = "",
        reflection: dict[str, str] | None = None,
    ) -> None: ...

    def post_task_entry(
        self, project_slug: str, task_id: int, body_markdown: str, *,
        reply_to_entry_id: int | None = None,
    ) -> dict[str, Any]: ...

    def post_project_entry(
        self, project_slug: str, body_markdown: str, *, reply_to_entry_id: int | None = None
    ) -> dict[str, Any]: ...


class LauncherProtocol(Protocol):
    def open(self, url: str) -> None: ...


class ExecutorProtocol(Protocol):
    def run(
        self,
        project_slug: str,
        project_path: Path,
        prompt_path: Path,
        prompt: str,
        execution_preferences: dict[str, str] | None = None,
    ) -> CodexExecResult: ...


ExecutorFactory = Callable[[BridgeConfig], ExecutorProtocol]
EXECUTION_BACKENDS: dict[str, ExecutorFactory] = {
    "codex": CodexExecRunner,
    "hermes": HermesExecRunner,
}


def register_execution_backend(name: str, factory: ExecutorFactory) -> None:
    """Register an executor adapter without changing the bridge controller."""
    backend = str(name or "").strip().lower()
    if not backend:
        raise ValueError("Execution backend name cannot be empty.")
    EXECUTION_BACKENDS[backend] = factory


def execution_preferences_for_entry(
    entry: dict[str, Any], backend: str
) -> dict[str, str]:
    """Return a validated, message-scoped execution override for one backend."""

    raw_metadata = entry.get("metadata_json")
    if isinstance(raw_metadata, dict):
        metadata = raw_metadata
    else:
        try:
            metadata = json.loads(str(raw_metadata or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    if not isinstance(metadata, dict):
        return {}
    execution = metadata.get("execution")
    if not isinstance(execution, dict):
        return {}
    selected = execution.get(str(backend or "").strip().lower())
    if not isinstance(selected, dict):
        return {}

    result: dict[str, str] = {}
    model = str(selected.get("model") or "").strip()
    if model and MODEL_ID_RE.fullmatch(model):
        result["model"] = model
    reasoning_effort = str(selected.get("reasoning_effort") or "").strip().lower()
    if reasoning_effort in REASONING_EFFORT_VALUES:
        result["reasoning_effort"] = reasoning_effort
    return result


class SubmitterProtocol(Protocol):
    def submit_after_delay(
        self,
        delay_seconds: float,
        hotkey: str,
        expected_prompt: str = "",
        reopen_callback: Callable[[], None] | None = None,
        model: str = "",
        reasoning_effort: str = "",
    ) -> bool | None: ...


@dataclass
class ProjectThreadRequest:
    project_slug: str
    project_path: Path
    prompt: str


@dataclass
class ProcessResult:
    device_label: str = ""
    task_count: int = 0
    mention_count: int = 0
    claimed_tasks: list[int] = field(default_factory=list)
    claimed_mentions: list[int] = field(default_factory=list)
    pending_tasks: list[int] = field(default_factory=list)
    pending_mentions: list[int] = field(default_factory=list)
    prompt_files: list[Path] = field(default_factory=list)
    opened_codex: list[str] = field(default_factory=list)
    executed_codex: list[CodexExecResult] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


class BridgeRunner:
    def __init__(
        self,
        config: BridgeConfig,
        client: HubClientProtocol | None = None,
        launcher: LauncherProtocol | None = None,
        executor: ExecutorProtocol | None = None,
        submitter: SubmitterProtocol | None = None,
        agent_decider: Any | None = None,
    ):
        self.config = config
        self.client = client or HubClient(
            config.hub,
            config.token,
            timeout_seconds=getattr(config, "hub_request_timeout_seconds", 12),
            get_retry_attempts=getattr(config, "hub_get_retry_attempts", 3),
            retry_backoff_seconds=getattr(
                config, "hub_retry_backoff_seconds", 1.0
            ),
            source_ip=getattr(config, "hub_source_ip", ""),
        )
        self.launcher = launcher or CodexLauncher(config.codex_exe_path)
        self.executor = executor or self._default_executor(config)
        self.agent_decider = agent_decider or CodexAgentDecisionRunner(config)
        self.submitter = submitter or CodexAppSubmitter(config.codex_exe_path)
        self.state = BridgeState.load(config.state_path)
        self._state_lock = threading.RLock()
        self._memory_sync_cache: dict[str, str] = {}
        self._capability_lock = threading.Lock()
        self._next_capability_report = 0.0
        self._model_catalog = CodexModelCatalogProvider(cli_path=config.codex_cli_path)

    @staticmethod
    def _default_executor(config: BridgeConfig) -> ExecutorProtocol:
        backend = str(getattr(config, "execution_backend", "codex")).lower()
        factory = EXECUTION_BACKENDS.get(backend)
        if factory is None:
            supported = ", ".join(sorted(EXECUTION_BACKENDS))
            raise ValueError(
                f"Unknown execution backend '{backend}'. Installed adapters: {supported}."
            )
        return factory(config)

    def process_once(self) -> ProcessResult:
        with self._state_lock:
            return self._process_once()

    def _process_once(self) -> ProcessResult:
        self._report_device_capabilities()
        inbox = self.client.get_inbox()
        tasks = list(inbox.get("tasks") or [])
        mentions = list(inbox.get("mentions") or [])
        device_label = str((inbox.get("device") or {}).get("label") or "")
        result = ProcessResult(
            device_label=device_label,
            task_count=len(tasks),
            mention_count=len(mentions),
        )
        self._recover_agent_executions(result)

        if not self.state.initialized and self.config.ignore_existing_on_first_run:
            self._seed_existing(tasks, mentions)
            result.messages.append("已记录当前收件箱状态。")
            self._save_state()
            return result

        visible_delivery_started = False
        for task in sorted(tasks, key=lambda item: int(item["id"])):
            prompt_count = len(result.prompt_files)
            self._process_task(task, device_label, result)
            if (
                self._verified_visible_submission_required()
                and len(result.prompt_files) > prompt_count
            ):
                visible_delivery_started = True
                break

        if not visible_delivery_started:
            for mention in sorted(mentions, key=lambda item: int(item["id"])):
                prompt_count = len(result.prompt_files)
                self._process_mention(mention, device_label, result)
                if (
                    self._verified_visible_submission_required()
                    and len(result.prompt_files) > prompt_count
                ):
                    break

        self.state.initialized = True
        self._save_state()
        return result

    def pending_mention_deliveries(self) -> list[dict[str, Any]]:
        if not self._state_lock.acquire(timeout=0.05):
            return []
        try:
            rows = []
            for key, delivery in self.state.mention_deliveries.items():
                row = dict(delivery)
                row["key"] = key
                rows.append(row)
            return sorted(
                rows,
                key=lambda item: (
                    str(item.get("project_slug") or ""),
                    int(item.get("mention_id") or 0),
                ),
            )
        finally:
            self._state_lock.release()

    def pending_task_deliveries(self) -> list[dict[str, Any]]:
        if not self._state_lock.acquire(timeout=0.05):
            return []
        try:
            rows = []
            for key, delivery in self.state.task_deliveries.items():
                row = dict(delivery)
                row["key"] = key
                rows.append(row)
            return sorted(
                rows,
                key=lambda item: (
                    str(item.get("project_slug") or ""),
                    int(item.get("task_id") or 0),
                ),
            )
        finally:
            self._state_lock.release()

    def confirm_pending_mention(self, project_slug: str, mention_id: int) -> None:
        with self._state_lock:
            pending = self.state.get_mention_delivery(project_slug, mention_id)
            if not pending:
                return
            self.state.set_mention_delivery(project_slug, mention_id, "submitted")
            self._save_state()
            transition = getattr(self.client, "mark_mention_submitted", None)
            if callable(transition):
                transition(project_slug, mention_id)
            self.client.mark_mention_handled(project_slug, mention_id)
            self.state.seen_mention_ids.add(str(mention_id))
            self.state.remove_mention_delivery(project_slug, mention_id)
            self._save_state()

    def retry_pending_mention(self, project_slug: str, mention_id: int) -> None:
        with self._state_lock:
            pending = self.state.get_mention_delivery(project_slug, mention_id)
            if not pending:
                return
            if str(pending.get("stage") or "") == "submitted":
                raise ValueError("该提及已经确认发送，只能补交处理确认，不能重新发送。")
            release = getattr(self.client, "release_mention_dispatch", None)
            if callable(release):
                release(project_slug, mention_id)
            self.state.remove_mention_delivery(project_slug, mention_id)
            self._save_state()

    def confirm_pending_task(self, project_slug: str, task_id: int) -> None:
        with self._state_lock:
            pending = self.state.get_task_delivery(project_slug, task_id)
            if not pending:
                return
            self.state.seen_task_ids.add(str(task_id))
            self.state.remove_task_delivery(project_slug, task_id)
            self._save_state()

    def retry_pending_task(self, project_slug: str, task_id: int) -> None:
        with self._state_lock:
            if not self.state.get_task_delivery(project_slug, task_id):
                return
            self.state.remove_task_delivery(project_slug, task_id)
            self._save_state()

    def poll_status(self) -> ProcessResult:
        """Refresh device presence without claiming or executing inbox work."""
        self._report_device_capabilities()
        inbox = self.client.get_inbox()
        return ProcessResult(
            device_label=str((inbox.get("device") or {}).get("label") or ""),
            task_count=len(inbox.get("tasks") or []),
            mention_count=len(inbox.get("mentions") or []),
        )

    def _report_device_capabilities(self) -> None:
        reporter = getattr(self.client, "post_device_capabilities", None)
        if not callable(reporter) or time.monotonic() < self._next_capability_report:
            return
        if not self._capability_lock.acquire(blocking=False):
            return
        try:
            if time.monotonic() < self._next_capability_report:
                return
            backend = str(self.config.execution_backend or "codex")
            mode = (f"{backend}-cli" if self.config.auto_execute_codex else
                    "codex-desktop" if self.config.open_codex else "manual")
            capabilities: dict[str, Any] = {"execution_backend": backend, "execution_mode": mode}
            if backend == "codex":
                capabilities["codex"] = self._model_catalog.get_catalog()
            reporter(capabilities)
            self._next_capability_report = time.monotonic() + 300
        except Exception:
            # An older/offline Hub must not disable inbox delivery. Retry the
            # optional capability announcement at a bounded rate.
            self._next_capability_report = time.monotonic() + 60
        finally:
            self._capability_lock.release()

    def _process_task(
        self, task: dict[str, Any], device_label: str, result: ProcessResult
    ) -> None:
        if task.get('assigned_agent_id') and callable(getattr(self.client, 'acquire_task_execution', None)):
            self._process_leased_agent_task(task, device_label, result)
            return
        task_id = int(task["id"])
        task_key = str(task_id)
        if task_key in self.state.seen_task_ids:
            return

        project_slug = str(task["project_slug"])
        pending = self.state.get_task_delivery(project_slug, task_id)
        if (pending and str(pending.get("stage", "")).startswith("agent_")
                and task.get("status") in {"done", "cancelled"}):
            # A human may have accepted/cancelled the work while this worker was
            # offline. Never overwrite that newer terminal decision on recovery.
            self.state.seen_task_ids.add(task_key)
            self.state.remove_task_delivery(project_slug, task_id)
            self._save_state()
            result.messages.append(f"任务 #{task_id} 已结束，保留 Hub 当前状态。")
            return
        if pending and pending.get("stage") == "agent_result":
            self._publish_agent_result(project_slug, task_id, result)
            return
        if pending and pending.get("stage") == "agent_decision_blocked":
            if not pending.get("status_reported"):
                self._publish_agent_decision_failure(project_slug, task_id)
                result.pending_tasks.append(task_id)
                return
            if task.get("status") == "open":
                # Explicitly re-opening an unexecuted task requests another decision.
                self.state.remove_task_delivery(project_slug, task_id)
                self._save_state()
                pending = None
        if pending:
            result.pending_tasks.append(task_id)
            result.messages.append(
                f"任务 #{task_id} 处于待确认状态，已阻止重复打开 Codex。"
            )
            return

        claimed = self.client.claim_task(project_slug, task_id)
        if not claimed:
            if not task.get("assigned_agent_id"):
                self.state.seen_task_ids.add(task_key)
            result.messages.append(f"任务 #{task_id} 无法领取。")
            return

        result.claimed_tasks.append(task_id)
        is_agent_task = bool(claimed.get("assigned_agent_id") or task.get("assigned_agent_id"))
        if is_agent_task and not self._prepare_agent_task(project_slug, task_id, result):
            return
        prompt = self.client.get_task_prompt(project_slug, task_id)
        if is_agent_task:
            prompt += self._agent_tool_instructions(project_slug, task_id)
        project_memory = self._prepare_project_memory(
            project_slug, device_label=device_label, result=result
        )
        if (
            project_memory
            and "# Project Memory:" not in prompt
            and "## Durable Project Memory" not in prompt
        ):
            prompt = "\n\n".join(
                ["## Durable Project Memory", project_memory.strip(), prompt.rstrip()]
            )
        codex_project_slug = self._target_project_slug_for_prompt(prompt, fallback=project_slug)
        attachments = PreparedAttachments()
        if is_agent_task:
            try:
                attachments = prepare_task_attachments(self.client, self.config, project_slug, task_id,
                                                      self._resolve_project_path(codex_project_slug))
                if attachments.images and str(getattr(self.config, 'execution_backend', 'codex')) != 'codex':
                    raise AttachmentPreparationError('当前执行器不支持任务图片输入，请使用 Codex CLI 连接。')
                prompt += attachments.prompt()
            except AttachmentPreparationError as exc:
                detail = '智能体尚未开始工作：' + str(exc)
                pending = self.state.set_task_delivery(project_slug, task_id, 'agent_decision_blocked', detail=detail)
                pending.update({'status_reported': False, 'failure_reason': '附件未能完整读取或校验；未运行工作进程。',
                                'next_time': '修复附件或连接后重新打开任务，再开始执行。'})
                self._save_state()
                self._publish_agent_decision_failure(project_slug, task_id)
                result.pending_tasks.append(task_id)
                result.messages.append(detail)
                return
        prompt_path = self._write_prompt_file(
            prefix=f"task-{task_id}-{project_slug}",
            body=prompt,
        )
        result.prompt_files.append(prompt_path)
        self.state.prompted_task_ids.add(task_key)

        if self.config.auto_execute_codex:
            if is_agent_task:
                # Persist before any writable process starts. A crash while the
                # process is running must not cause a second execution on restart.
                self.state.set_task_delivery(project_slug, task_id, "agent_running",
                                             detail="智能体工作进程已开始，禁止重复执行。")
                self._save_state()
            self.client.update_task_status(
                project_slug,
                task_id,
                "in_progress",
                "CodexBridge 已开始自动执行。",
            )
            exec_result = self._execute_codex(codex_project_slug, prompt_path, prompt,
                                             image_paths=attachments.images)
            result.executed_codex.append(exec_result)
            if is_agent_task:
                self._save_agent_result(project_slug, task_id, exec_result)
                self._publish_agent_result(project_slug, task_id, result)
            else:
                self._write_task_exec_result(
                    project_slug,
                    task_id,
                    exec_result,
                    primary_device_label=str(task.get("primary_device_label") or ""),
                    device_label=device_label,
                )
        else:
            if self._verified_visible_submission_required():
                # Persist before opening the deep link. A crash or UI timeout
                # must fail closed instead of opening the same task again.
                self.state.set_task_delivery(
                    project_slug,
                    task_id,
                    "dispatching",
                    detail="已开始可见投递。",
                )
                self._save_state()
            opened = self._open_codex(codex_project_slug, prompt_path, prompt, result)
            if opened:
                result.opened_codex.append(opened)
            elif self._verified_visible_submission_required():
                detail = str(
                    getattr(self.submitter, "last_error", "")
                    or "未确认草稿已提交"
                )
                send_attempted = bool(
                    getattr(self.submitter, "last_send_attempted", False)
                )
                self.state.set_task_delivery(
                    project_slug,
                    task_id,
                    "uncertain",
                    detail=detail,
                )
                self._save_state()
                result.pending_tasks.append(task_id)
                if send_attempted:
                    result.messages.append(
                        f"任务 #{task_id} 已执行发送动作但结果不确定；"
                        "已锁定为待确认，不会自动重发。"
                    )
                else:
                    result.messages.append(
                        f"任务 #{task_id} 未执行发送动作；已锁定为待确认，"
                        "不会自动重新打开 Codex。确认未发送后可人工重新投递。"
                    )
                return

        self.state.seen_task_ids.add(task_key)
        self.state.remove_task_delivery(project_slug, task_id)

    def _reject_agent_receipt(self, slug: str, task_id: int, detail: str, result: ProcessResult) -> None:
        self.state.set_task_delivery(slug, task_id, 'agent_result_rejected', detail=detail)
        self.state.seen_task_ids.add(str(task_id))
        self._save_state()
        result.messages.append(f'任务 #{task_id}：{detail}')

    def _recover_agent_executions(self, result: ProcessResult) -> None:
        if not callable(getattr(self.client, 'reconcile_task_execution', None)):
            return
        # Completed/cancelled tasks disappear from inbox, but their local receipts
        # must still be reconciled after a lost acknowledgement or worker restart.
        for receipt in list(self.state.task_deliveries.values()):
            if not receipt.get('run_key') or receipt.get('stage') == 'agent_result_rejected':
                continue
            slug, task_id = receipt['project_slug'], int(receipt['task_id'])
            try:
                remote = self.client.reconcile_task_execution(slug, task_id)
                current, task = remote.get('execution'), remote.get('task') or {}
                if not current:
                    if receipt.get('stage') == 'agent_acquiring':
                        continue  # The acquire POST may never have reached Hub.
                    self._reject_agent_receipt(slug, task_id, '执行记录缺失，保留本地收据等待核对。', result)
                    continue
                if current.get('run_key') != receipt['run_key']:
                    self._reject_agent_receipt(slug, task_id, '已有其他执行代际，旧收据不会覆盖任务。', result)
                    continue
                receipt['execution'] = current
                receipt['model_selection'] = remote.get('model_selection') or receipt.get('model_selection') or {}
                if current['state'] == 'completed':
                    self.state.seen_task_ids.add(str(task_id))
                    self.state.remove_task_delivery(slug, task_id)
                    self._save_state()
                    continue
                if task.get('status') in {'done', 'cancelled'} or current['task_version'] != task.get('status_version'):
                    self._reject_agent_receipt(slug, task_id, '任务已取消、结束或被更新；执行结果保留本地。', result)
                    continue
                if current['state'] == 'prepared':
                    # The server has never granted a start. Even an uncertain
                    # start POST is safe to retry only after this verified state.
                    receipt['stage'] = 'agent_prepared'
                    self._save_state()
                    continue
                if receipt.get('stage') != 'agent_result':
                    completion = read_execution_completion(Path(receipt.get('completion_path') or ''), current)
                    if completion:
                        recovered = CodexExecResult(ok=completion['ok'], command=[],
                            returncode=completion['returncode'], stdout='', stderr='',
                            last_message=completion['last_message'], error=completion['error'],
                            output_path=Path(completion.get('output_path') or receipt['completion_path']))
                        self._save_agent_result(slug, task_id, recovered)
                    else:
                        result.pending_tasks.append(task_id)
                        continue
                self._publish_agent_result(slug, task_id, result)
            except Exception as exc:
                if getattr(exc, 'status_code', None) in {403,404,409}:
                    self._reject_agent_receipt(slug, task_id, 'Hub 拒绝旧执行凭据，已保留本地收据。', result)
                else:
                    result.messages.append(f'任务 #{task_id} 的收据核对暂未完成，未重新执行。')

    def _process_leased_agent_task(self, task: dict, device_label: str, result: ProcessResult) -> None:
        slug, task_id = str(task['project_slug']), int(task['id'])
        if str(task_id) in self.state.seen_task_ids:
            return
        receipt = self.state.get_task_delivery(slug, task_id)
        if receipt and receipt.get('stage') == 'agent_result':
            self._publish_agent_result(slug, task_id, result)
            return
        if receipt and receipt.get('stage') == 'agent_decision_blocked':
            if not receipt.get('status_reported'):
                self._publish_agent_decision_failure(slug, task_id)
                return
            if task.get('status') != 'open':
                return
            self.state.remove_task_delivery(slug, task_id)
            receipt = None
        if receipt and receipt.get('stage') not in {'agent_acquiring','agent_prepared'}:
            result.pending_tasks.append(task_id)
            return
        if not receipt:
            if task.get('status') not in {'open', 'claimed'}:
                # Pre-upgrade in-progress work has no proof it is safe to repeat.
                result.pending_tasks.append(task_id)
                return
            if not self._prepare_agent_task(slug, task_id, result):
                return
            try:
                from .model_allocator import allocate_task_model
                allocate_task_model(self.config, self.client, slug, task_id)
            except Exception as exc:
                self.state.set_task_delivery(slug, task_id, 'agent_decision_blocked',
                    detail='智能体尚未开始工作：任务模型选择未完成。' + str(exc)[:500])
                self._save_state()
                self._publish_agent_decision_failure(slug, task_id)
                return
            prompt = self.client.get_task_prompt(slug, task_id) + self._agent_tool_instructions(slug, task_id)
            memory = self._prepare_project_memory(slug, device_label=device_label, result=result)
            if memory and '# Project Memory:' not in prompt and '## Durable Project Memory' not in prompt:
                prompt = '## Durable Project Memory\n' + memory.strip() + '\n\n' + prompt
            target = self._target_project_slug_for_prompt(prompt, fallback=slug)
            try:
                attachments = prepare_task_attachments(self.client, self.config, slug, task_id, self._resolve_project_path(target))
                if str(getattr(self.config, 'execution_backend', 'codex')) != 'codex':
                    raise AttachmentPreparationError('持久智能体执行需使用 Codex CLI。')
            except AttachmentPreparationError as exc:
                self.state.set_task_delivery(slug, task_id, 'agent_decision_blocked', detail='智能体尚未开始工作：' + str(exc))
                self._save_state()
                self._publish_agent_decision_failure(slug, task_id)
                return
            prompt += attachments.prompt()
            prompt_path = self._write_prompt_file(prefix=f'task-{task_id}-{slug}', body=prompt)
            result.prompt_files.append(prompt_path)
            receipt = self.state.set_task_delivery(slug, task_id, 'agent_acquiring')
            receipt.update(run_key=uuid.uuid4().hex, prompt_path=str(prompt_path),
                           target_project=target, image_paths=[str(p) for p in attachments.images])
            self._save_state()  # Stable identity precedes the first network POST.
        try:
            if receipt.get('stage') == 'agent_prepared' and receipt['execution']['lease_until'] <= time.time():
                receipt.update(stage='agent_acquiring', run_key=uuid.uuid4().hex)
                receipt.pop('execution', None)
                self._save_state()
            if receipt.get('stage') == 'agent_acquiring':
                acquired = self.client.acquire_task_execution(slug, task_id, receipt['run_key'])
                execution = acquired.get('execution') or {}
                if execution.get('state') != 'prepared' or execution.get('run_key') != receipt['run_key']:
                    result.pending_tasks.append(task_id)
                    return
                receipt.update(execution=execution, model_selection=acquired.get('model_selection') or {}, stage='agent_prepared')
                self._save_state()
            execution = receipt['execution']
            completion_path = self.config.codex_exec_output_dir / slug / f'task-{task_id}-g{execution["generation"]}-{receipt["run_key"]}.completion.json'
            receipt.update(stage='agent_start_requested', completion_path=str(completion_path))
            self._save_state()
            started = self.client.start_task_execution(slug, task_id, execution)
            receipt.update(stage='agent_running', execution=started['execution'])
            self._save_state()  # No writable process may start before this save.
        except Exception as exc:
            result.pending_tasks.append(task_id)
            code = getattr(exc, 'status_code', None)
            receipt['detail'] = '开始许可尚未确认' + (f'（Hub {code}）' if code else '') + '，未启动或重复执行。'
            self._save_state()
            result.messages.append(f'任务 #{task_id}：' + receipt['detail'])
            return
        context = dict(receipt['execution'], task_id=task_id, project_slug=slug, receipt_path=str(completion_path))
        prompt_path = Path(receipt['prompt_path'])
        # Fetch after start is fenced: amendments to queued work cannot leave an old prompt running.
        try:
            fresh_prompt = self.client.get_task_prompt(slug, task_id) + self._agent_tool_instructions(slug, task_id)
            prepared = prepare_task_attachments(self.client, self.config, slug, task_id, self._resolve_project_path(receipt['target_project']))
            fresh_prompt += prepared.prompt()
            prompt_path.write_text(fresh_prompt,encoding='utf-8')
            receipt['image_paths']=[str(path) for path in prepared.images]
            self._save_state()
        except Exception:
            failure=CodexExecResult(ok=False,command=[],returncode=None,stdout='',stderr='',
                last_message='执行前核对最新要求或附件失败，未启动工作进程。',output_path=prompt_path,
                error='执行前核对最新要求或附件失败，未启动工作进程。')
            self._save_agent_result(slug,task_id,failure)
            self._publish_agent_result(slug,task_id,result)
            return
        with ExecutionRenewal(self.client, slug, task_id, receipt['execution']) as renewal:
            exec_result = self._execute_codex(receipt['target_project'], prompt_path,
                prompt_path.read_text(encoding='utf-8'), execution_preferences=receipt.get('model_selection'),
                image_paths=[Path(p) for p in receipt['image_paths']], execution_context=context,
                cancel_event=renewal.cancelled)
        result.executed_codex.append(exec_result)
        self._save_agent_result(slug, task_id, exec_result)
        self._publish_agent_result(slug, task_id, result)

    def _prepare_agent_task(self, project_slug: str, task_id: int, result: ProcessResult) -> bool:
        try:
            if not self.config.auto_execute_codex:
                raise ValueError("当前执行连接尚未启用 CLI 自动执行。")
            context = self.client.get_agent_context(project_slug, task_id)
            if not context.get("agent"):
                raise ValueError("Hub 未返回已分配的智能体身份。")
            if (context.get("task") or {}).get("conversation_id"):
                return True
            try:
                decision = self.agent_decider.decide(context)
                routed = self.client.route_agent_task(project_slug, task_id, decision)
            except Exception:
                # Only the planning phase falls back. Never rerun the actual
                # task through another backend after uncertain execution.
                routed = self.client.fallback_agent_route(project_slug, task_id)
            if not (routed.get("task") or {}).get("conversation_id"):
                raise ValueError("工作对话尚未确定。")
            return True
        except Exception as exc:
            detail = "智能体尚未开始工作：" + str(exc)[:700]
            pending = self.state.set_task_delivery(project_slug, task_id, "agent_decision_blocked", detail=detail)
            pending["status_reported"] = False
            self._save_state()
            self._publish_agent_decision_failure(project_slug, task_id)
            result.pending_tasks.append(task_id)
            result.messages.append(detail)
            return False

    def _publish_agent_decision_failure(self, project_slug: str, task_id: int) -> None:
        pending = self.state.get_task_delivery(project_slug, task_id)
        if not pending or pending.get("status_reported"):
            return
        detail = pending["detail"]
        self.client.update_task_status(project_slug, task_id, "blocked", detail,
            reflection={"summary": detail, "worked": "",
                        "failed": pending.get('failure_reason', "执行前决策或连接不可用；未运行工作进程。"),
                        "next_time": pending.get('next_time', "恢复决策连接后重新打开任务，再开始执行。")})
        pending["status_reported"] = True
        self._save_state()

    def _agent_tool_instructions(self, project_slug: str, task_id: int) -> str:
        def quote(value: str) -> str:
            return "'" + value.replace("'", "''") + "'"

        binding = str(getattr(self.config, "binding_id", "") or "")
        command = f"& {quote(sys.executable)} -X utf8 -m codex_bridge.agent_runtime --project {quote(project_slug)} --task-id {task_id}"
        helper_config = os.environ.get("CODEX_BRIDGE_AGENT_CONFIG", "")
        if helper_config:
            command += " --config " + quote(helper_config)
        if binding:
            command += " --binding-id " + quote(binding)
        return (
            "\n\n## Hub 智能体工作接口\n"
            "用 UTF-8 文件提交结构化请求，通过现有连接读取身份和委派任务；不要读取、输出或复制连接 token。\n"
            f"PowerShell 调用：{command} --request-file '<请求 JSON 文件绝对路径>'\n"
            f"总控调度通道请直接使用：{command} --request-json '<JSON对象>'，不需写请求文件。\n"
            "总控运行于独立调度目录；实际项目文件修改必须委派执行者。不得绕过目录限制修改原项目。\n"
            '查看当前身份、经验、团队和任务：{"action":"inspect"}。\n'
            '总控调整当前对话已有工作：{"action":"revise","target_task_id":真实任务编号,"request_key":"stable-change-key","instruction":"最新具体修改要求"}。'
            '返回 updated 表示待办已更新；queued 表示待原工作结束后接续，不能声称执行者已经按修改完成。\n'
            '按需查看某个智能体实际可用模型：{"action":"model_options","agent_id":智能体数字编号}。\n'
            '总控委派示例：{"action":"delegate","request_key":"stable-subtask-key",'
            '"assigned_agent_id":目标智能体数字编号,"title":"明确交付结果",'
            '"description":"任务范围、输入与验收要求","priority":"normal"}。\n'
            '委派默认继承当前源任务的附件；可加 "attachment_ids":[附件数字编号] 只传必要文件，'
            '或 "attachment_ids":[] 不传附件。只可选择当前任务清单里的编号，不能引用项目其他文件。\n'
            '总控可在委派请求加 "model_selection":{"model":"实际目录中的模型ID",'
            '"reasoning_effort":"medium 或 high","reason":"结合任务复杂度选择的理由"}；'
            '用户手动指定的模型优先，使用接口返回的实际选择，不猜测不存在的模型名。\n'
            "先 inspect 取得实际编号，不能猜编号；同一委派重试保持同一个 request_key。"
            "只有接口返回任务编号才算已分配。总控可在本任务授权范围内委派其他智能体；"
            "仍需汇报哪些已交付、哪些还在执行，不得把成功创建任务写成全部完成。"
            "工作结果与结构化复盘由 Bridge 回传，勿重复自行发回同一结果。\n"
            '交付文件放在项目 output/hub-task-'+str(task_id)+'/，最终答复给出明确 Markdown 文件链接，'
            '并追加 <hub-artifacts>[{"path":"项目内交付文件路径"}]</hub-artifacts>（最多12个、每个50MB、合计100MB）。'
            'Bridge会上传到Hub并展示预览与下载；不需要SW公开上传，不要只报一个目录或称本地路径已可在网页打开。\n'
        )

    def _save_agent_result(self, project_slug: str, task_id: int, execution: CodexExecResult) -> None:
        body, reflection, complete = extract_reflection(execution.last_message or execution.summary, ok=execution.ok)
        summary = body or reflection["summary"] or execution.summary
        if not execution.ok:
            summary = "自动执行失败：" + execution.summary + "\n" + body
        if not complete:
            summary += "\n结构化复盘尚未生成，已保留执行结果供后续补充。"
        receipt = self.state.set_task_delivery(project_slug, task_id, "agent_result",
                                               detail="执行已结束，正在回传结果与经验。")
        receipt.update({"result_summary": summary[:MAX_HUB_RESULT_CHARS], "reflection": reflection,
                        "ok": execution.ok, "output_path": str(execution.output_path),
                        "result_status": "blocked" if not execution.ok or reflection.get("outcome") == "blocked" else "review",
                        "entry_attempted": False, "reflection_complete": complete})
        self._save_state()

    def _publish_agent_result(self, project_slug: str, task_id: int, result: ProcessResult) -> None:
        receipt = self.state.get_task_delivery(project_slug, task_id)
        if not receipt or receipt.get("stage") != "agent_result":
            return
        summary = str(receipt.get("result_summary") or "")
        status = receipt.get("result_status") or ("review" if receipt["ok"] else "blocked")
        if receipt.get('execution'):
            body = summary
            try:
                if not receipt.get('artifact_publish_failed') and callable(getattr(self.client,'upload_task_artifact',None)):
                    from .artifacts import publish_artifacts
                    body=publish_artifacts(self.client,project_slug,task_id,receipt['execution'],
                        self._resolve_project_path(receipt.get('target_project') or project_slug),body)
                self.client.complete_task_execution(project_slug, task_id, receipt['execution'],
                    status=status, summary=body[:1000], body=body, reflection=receipt['reflection'])
            except Exception as exc:
                if isinstance(exc, ValueError) or getattr(exc,'status_code',None) in {413,415,422}:
                    # Invalid delivery cannot heal through endless network retries.
                    # Preserve the local execution output and publish an honest blocker once.
                    receipt['artifact_publish_failed']=True
                    receipt['result_status']='blocked'
                    receipt['result_summary']=re.sub(r'<hub-artifacts>.*?</hub-artifacts>','',summary,flags=re.S).strip()+'\n\n成果尚未发布：文件清单、格式或大小不符合上传要求。原执行结果已保留，需要修正交付文件后继续；不能把本地路径当作网页成果。'
                    receipt['reflection']={**receipt['reflection'],'failed':'成果文件未通过上传校验','next_time':'交付前检查清单、文件存在性和大小限制'}
                    self._save_state()
                    return self._publish_agent_result(project_slug,task_id,result)
                if getattr(exc, 'status_code', None) in {403,404,409}:
                    self._reject_agent_receipt(project_slug, task_id, '任务状态或执行代际已改变，结果保留本地，未覆盖 Hub。', result)
                else:
                    result.pending_tasks.append(task_id)
                    result.messages.append(f'任务 #{task_id} 的结果已保存本地，恢复连接后核对回传，未重复执行。')
                return
            self.state.seen_task_ids.add(str(task_id))
            self.state.remove_task_delivery(project_slug, task_id)
            self._save_state()
            return
        self.client.update_task_status(project_slug, task_id, status,
                                       summary[:1000], reflection=receipt["reflection"])
        if not receipt.get("entry_attempted"):
            # Task state and reflection are idempotent; old entry endpoints are
            # not. Save the attempt first, so an unknown acknowledgement cannot
            # duplicate a visible result entry during recovery.
            receipt["entry_attempted"] = True
            self._save_state()
            try:
                self.client.post_task_entry(project_slug, task_id,
                    f"## {self._executor_label()} 智能体任务 #{task_id}\n\n"
                    f"- 状态：{'待复核' if status == 'review' else '遇阻'}\n\n{summary}")
            except Exception:
                result.messages.append(f"任务 #{task_id} 的状态与经验已保存；详细消息回传未确认，未重复发送。")
        if not receipt.get("reflection_complete"):
            result.messages.append(f"任务 #{task_id} 已记录实际结果，结构化复盘待补充。")
        self.state.seen_task_ids.add(str(task_id))
        self.state.remove_task_delivery(project_slug, task_id)
        self._save_state()

    def _process_mention(
        self, mention: dict[str, Any], device_label: str, result: ProcessResult
    ) -> None:
        mention_id = int(mention["id"])
        mention_key = str(mention_id)
        if mention_key in self.state.seen_mention_ids:
            return

        project_slug = str(mention["project_slug"])
        pending = self.state.get_mention_delivery(project_slug, mention_id)
        if pending:
            stage = str(pending.get("stage") or "dispatching")
            if stage == "submitted":
                result.messages.append(
                    f"提及 #{mention_id} 已发送，正在补交 Hub 处理确认。"
                )
                self._finalize_mention(project_slug, mention_id, result)
            else:
                result.pending_mentions.append(mention_id)
                result.messages.append(
                    f"提及 #{mention_id} 处于待确认状态，已阻止重复打开 Codex。"
                )
            return

        remote_status = str(mention.get("claim_status") or "unclaimed")
        if remote_status == "submitted":
            self.state.set_mention_delivery(
                project_slug,
                mention_id,
                "submitted",
                delivery_id=str(mention.get("delivery_id") or ""),
            )
            self._save_state()
            self._finalize_mention(project_slug, mention_id, result)
            return
        if remote_status == "dispatching":
            self.state.set_mention_delivery(
                project_slug,
                mention_id,
                "uncertain",
                delivery_id=str(mention.get("delivery_id") or ""),
                detail="Hub 已记录该提及正在投递。",
            )
            self._save_state()
            result.pending_mentions.append(mention_id)
            result.messages.append(
                f"提及 #{mention_id} 已在其他投递流程中，已阻止重复打开 Codex。"
            )
            return

        claimed = self.client.claim_mention(project_slug, mention_id)
        if not claimed:
            result.messages.append(f"提及 #{mention_id} 无法领取。")
            return

        claimed_status = str(claimed.get("claim_status") or "claimed")
        if claimed_status in {"dispatching", "submitted"}:
            stage = "submitted" if claimed_status == "submitted" else "uncertain"
            self.state.set_mention_delivery(
                project_slug,
                mention_id,
                stage,
                delivery_id=str(claimed.get("delivery_id") or ""),
                detail="Hub 已存在投递回执。" if stage == "uncertain" else "",
            )
            self._save_state()
            if stage == "submitted":
                self._finalize_mention(project_slug, mention_id, result)
            else:
                result.pending_mentions.append(mention_id)
                result.messages.append(
                    f"提及 #{mention_id} 已在投递中，已阻止重复打开 Codex。"
                )
            return

        mention = {**mention, **claimed}
        if mention_delivery_mode(mention) == "selected":
            mention["primary_device_label"] = ""
        result.claimed_mentions.append(mention_id)
        self._start_mention_delivery(project_slug, mention_id, claimed, result)
        project_memory = self._prepare_project_memory(
            project_slug, device_label=device_label, result=result
        )
        mention_prompt = self._build_mention_prompt(
            mention, device_label, project_memory=project_memory
        )
        prompt_path = self._write_prompt_file(
            prefix=f"mention-{mention_id}-{project_slug}",
            body=mention_prompt,
        )
        result.prompt_files.append(prompt_path)
        self.state.prompted_mention_ids.add(mention_key)

        thread_request = self._extract_project_thread_request(claimed)
        codex_preferences = execution_preferences_for_entry(claimed, "codex")
        backend_preferences = execution_preferences_for_entry(
            claimed, str(getattr(self.config, "execution_backend", "codex"))
        )
        if thread_request:
            opened = self._open_codex_thread(
                thread_request.project_path,
                thread_request.prompt,
                result,
                execution_preferences=codex_preferences,
            )
            if opened:
                result.opened_codex.append(opened)
            elif self._verified_visible_submission_required():
                self._handle_failed_visible_submission(
                    project_slug, mention_id, result
                )
                return
            self._mark_mention_submitted(project_slug, mention_id, result)
            self._post_mention_reply(
                mention,
                self._with_primary_device_mention(
                    "\n".join(
                        [
                            f"{device_label} 已打开 Codex 新对话（提及 #{mention_id}）。",
                            "",
                            f"- 目标项目：`{thread_request.project_slug}`",
                            f"- 本地路径：`{thread_request.project_path}`",
                        ]
                    ),
                    primary_device_label=str(mention.get("primary_device_label") or ""),
                    device_label=device_label,
                ),
            )
            self.state.acked_mention_ids.add(mention_key)
            self._finalize_mention(project_slug, mention_id, result)
            return

        if self.config.auto_execute_codex:
            exec_result = self._execute_codex(
                project_slug,
                prompt_path,
                mention_prompt,
                execution_preferences=backend_preferences,
            )
            result.executed_codex.append(exec_result)
            self._mark_mention_submitted(project_slug, mention_id, result)
            self._post_mention_reply(
                mention,
                self._with_primary_device_mention(
                    self._format_exec_entry(
                        title=f"{self._executor_label()} 自动执行提及 #{mention_id}",
                        exec_result=exec_result,
                    ),
                    primary_device_label=str(mention.get("primary_device_label") or ""),
                    device_label=device_label,
                ),
            )
            self.state.acked_mention_ids.add(mention_key)
        else:
            opened = self._open_codex(
                project_slug,
                prompt_path,
                mention_prompt,
                result,
                execution_preferences=codex_preferences,
            )
            if opened:
                result.opened_codex.append(opened)
            elif self._verified_visible_submission_required():
                self._handle_failed_visible_submission(
                    project_slug, mention_id, result
                )
                return

            self._mark_mention_submitted(project_slug, mention_id, result)

            if self.config.ack_mentions:
                ack = (
                    f"{device_label} 已收到提及 #{mention_id}。"
                    "本地桥接已在 Codex 中打开。"
                )
                self._post_mention_reply(
                    mention,
                    self._with_primary_device_mention(
                        ack,
                        primary_device_label=str(mention.get("primary_device_label") or ""),
                        device_label=device_label,
                    ),
                )
                self.state.acked_mention_ids.add(mention_key)

        self._finalize_mention(project_slug, mention_id, result)

    def _post_mention_reply(self, mention: dict[str, Any], body: str) -> dict[str, Any]:
        project_slug = str(mention["project_slug"])
        reply_id = int(mention["id"])
        if mention.get("task_id") is not None:
            return self.client.post_task_entry(
                project_slug, int(mention["task_id"]), body, reply_to_entry_id=reply_id
            )
        return self.client.post_project_entry(project_slug, body, reply_to_entry_id=reply_id)

    def _start_mention_delivery(
        self,
        project_slug: str,
        mention_id: int,
        mention: dict[str, Any],
        result: ProcessResult,
    ) -> None:
        delivery_id = str(mention.get("delivery_id") or "")
        self.state.set_mention_delivery(
            project_slug, mention_id, "dispatching", delivery_id=delivery_id
        )
        self._save_state()
        transition = getattr(self.client, "mark_mention_dispatching", None)
        if not callable(transition):
            return
        try:
            receipt = transition(project_slug, mention_id) or {}
            server_delivery_id = str(receipt.get("delivery_id") or delivery_id)
            if server_delivery_id != delivery_id:
                self.state.set_mention_delivery(
                    project_slug,
                    mention_id,
                    "dispatching",
                    delivery_id=server_delivery_id,
                )
                self._save_state()
        except Exception as exc:
            result.messages.append(
                f"提及 #{mention_id} 的 Hub 投递状态暂未同步；本地防重复锁已生效：{exc}"
            )

    def _mark_mention_submitted(
        self, project_slug: str, mention_id: int, result: ProcessResult
    ) -> None:
        self.state.set_mention_delivery(project_slug, mention_id, "submitted")
        self._save_state()
        transition = getattr(self.client, "mark_mention_submitted", None)
        if not callable(transition):
            return
        try:
            transition(project_slug, mention_id)
        except Exception as exc:
            result.messages.append(
                f"提及 #{mention_id} 已发送；Hub 提交状态将在下一轮补交：{exc}"
            )

    def _handle_failed_visible_submission(
        self, project_slug: str, mention_id: int, result: ProcessResult
    ) -> None:
        detail = str(getattr(self.submitter, "last_error", "") or "未确认草稿已提交")
        send_attempted = bool(
            getattr(self.submitter, "last_send_attempted", False)
        )
        self.state.set_mention_delivery(
            project_slug,
            mention_id,
            "uncertain",
            detail=detail,
        )
        self._save_state()
        result.pending_mentions.append(mention_id)
        if send_attempted:
            result.messages.append(
                f"提及 #{mention_id} 已执行发送动作但结果不确定；已锁定为待确认，不会自动重发。"
            )
        else:
            result.messages.append(
                f"提及 #{mention_id} 未执行发送动作；已锁定为待确认，"
                "不会自动重新打开 Codex。确认未发送后可人工重新投递。"
            )

    def _finalize_mention(
        self, project_slug: str, mention_id: int, result: ProcessResult
    ) -> bool:
        try:
            self.client.mark_mention_handled(project_slug, mention_id)
        except Exception as exc:
            self.state.set_mention_delivery(project_slug, mention_id, "submitted")
            self._save_state()
            result.pending_mentions.append(mention_id)
            result.messages.append(
                f"提及 #{mention_id} 已发送，Hub 处理确认失败；只会重试确认，不会重发：{exc}"
            )
            return False
        self.state.seen_mention_ids.add(str(mention_id))
        self.state.remove_mention_delivery(project_slug, mention_id)
        self._save_state()
        return True

    def _execute_codex(
        self,
        project_slug: str,
        prompt_path: Path,
        prompt_body: str,
        execution_preferences: dict[str, str] | None = None,
        image_paths: list[Path] | None = None,
        execution_context: dict | None = None,
        cancel_event: threading.Event | None = None,
    ) -> CodexExecResult:
        project_path = self._resolve_project_path(project_slug)
        if not project_path:
            output_path = self.config.codex_exec_output_dir / project_slug / (
                f"{prompt_path.stem}-missing-project.md"
            )
            return CodexExecResult(
                ok=False,
                command=[],
                returncode=None,
                stdout="",
                stderr="",
                last_message="",
                output_path=output_path,
                error=f"项目 {project_slug} 未配置本地文件夹。",
            )

        prompt = self._build_codex_exec_prompt(prompt_path, prompt_body)
        if execution_preferences or image_paths or execution_context:
            options: dict[str, Any] = {}
            if execution_preferences:
                options['execution_preferences'] = execution_preferences
            if image_paths:
                options['image_paths'] = image_paths
            if execution_context:
                options['execution_context'] = execution_context
            if cancel_event is not None:
                options['cancel_event'] = cancel_event
            return self.executor.run(
                project_slug,
                project_path,
                prompt_path,
                prompt,
                **options,
            )
        return self.executor.run(project_slug, project_path, prompt_path, prompt)

    def _extract_project_thread_request(
        self, mention: dict[str, Any]
    ) -> ProjectThreadRequest | None:
        body = str(mention.get("body_markdown") or "")
        match = PROJECT_THREAD_REQUEST_RE.search(body)
        if not match:
            return None

        project_name = match.group(1).strip()
        mapped = self._project_mapping_for_name(project_name)
        if not mapped:
            return None

        prompt_match = THREAD_PROMPT_RE.search(body[match.end() :])
        if not prompt_match:
            prompt_match = THREAD_PROMPT_RE.search(body)
        prompt = prompt_match.group(1).strip() if prompt_match else ""
        if not prompt:
            prompt = body[match.end() :].strip(" ，。:：\r\n\t")
        prompt = prompt or "请根据 Hub 提及继续处理。"
        return ProjectThreadRequest(
            project_slug=mapped[0],
            project_path=mapped[1].expanduser().resolve(),
            prompt=prompt,
        )

    def _project_mapping_for_name(self, name: str) -> tuple[str, Path] | None:
        target = name.strip().lower()
        for slug, path in self.config.projects.items():
            if slug.lower() == target:
                resolved = self._resolve_project_path(slug)
                return (slug, resolved) if resolved else None
        resolved = self._resolve_project_path(target)
        if resolved:
            return target, resolved
        return None

    def _write_task_exec_result(
        self,
        project_slug: str,
        task_id: int,
        exec_result: CodexExecResult,
        primary_device_label: str = "",
        device_label: str = "",
    ) -> None:
        if exec_result.ok:
            self.client.update_task_status(
                project_slug,
                task_id,
                "review",
                trim_text(exec_result.summary, 1000),
            )
        else:
            self.client.update_task_status(
                project_slug,
                task_id,
                "review",
                trim_text(f"自动执行失败：{exec_result.summary}", 1000),
            )
        self.client.post_task_entry(
            project_slug,
            task_id,
            self._format_exec_entry(
                title=f"{self._executor_label()} 自动执行任务 #{task_id}",
                exec_result=exec_result,
            ),
        )

    def _with_primary_device_mention(
        self, body: str, primary_device_label: str = "", device_label: str = ""
    ) -> str:
        return apply_device_mention_policy(
            body,
            primary_device_label=primary_device_label,
            current_device_label=device_label,
        )

    def _format_exec_entry(self, title: str, exec_result: CodexExecResult) -> str:
        status = "完成" if exec_result.ok else "失败"
        body = trim_text(exec_result.summary, MAX_HUB_RESULT_CHARS)
        lines = [
            f"## {title}",
            "",
            f"- 状态：{status}",
            f"- 输出文件：`{exec_result.output_path}`",
            "",
            body,
        ]
        return "\n".join(lines).strip()

    def _executor_label(self) -> str:
        if getattr(self.config, "execution_backend", "codex") == "hermes":
            return "HermesBridge"
        return "CodexBridge"

    def _open_codex(
        self,
        project_slug: str,
        prompt_path: Path,
        prompt_body: str,
        result: ProcessResult | None = None,
        execution_preferences: dict[str, str] | None = None,
    ) -> str:
        if not self.config.open_codex:
            return ""
        project_path = self._resolve_project_path(project_slug)
        if not project_path:
            return ""
        prompt = self._build_codex_launch_prompt(prompt_path, prompt_body)
        url = "codex://new?" + urlencode(
            {"path": str(project_path), "prompt": prompt}
        )
        self.launcher.open(url)
        submitted = self._auto_submit_if_enabled(
            result,
            expected_prompt=prompt,
            reopen_callback=lambda: self.launcher.open(url),
            execution_preferences=execution_preferences,
        )
        if submitted is False:
            return ""
        return url

    def _target_project_slug_for_prompt(self, prompt_body: str, fallback: str) -> str:
        match = CODEX_TARGET_PROJECT_RE.search(prompt_body)
        if not match:
            return fallback
        target = match.group(1).strip().lower()
        return target if PROJECT_SLUG_RE.fullmatch(target) else fallback

    def _resolve_project_path(self, project_slug: str) -> Path | None:
        slug = project_slug.strip().lower()
        if not PROJECT_SLUG_RE.fullmatch(slug):
            return None
        configured = self.config.projects.get(slug)
        if configured:
            return self._ensure_project_directory(slug, configured)
        if not getattr(self.config, "auto_create_missing_projects", False):
            return None
        root = getattr(self.config, "project_root", None)
        if not root:
            return None
        root_path = Path(root).expanduser().resolve()
        target_path = (root_path / slug).resolve()
        try:
            target_path.relative_to(root_path)
        except ValueError:
            return None
        return self._ensure_project_directory(slug, target_path)

    def _ensure_project_directory(self, slug: str, path: Path) -> Path | None:
        resolved = path.expanduser().resolve()
        if resolved.exists() and not resolved.is_dir():
            return None
        if not resolved.exists():
            if not getattr(self.config, "auto_create_missing_projects", False):
                return None
            resolved.mkdir(parents=True, exist_ok=True)
        self.config.projects.setdefault(slug, resolved)
        return resolved

    def _open_codex_thread(
        self,
        project_path: Path,
        prompt: str,
        result: ProcessResult | None = None,
        execution_preferences: dict[str, str] | None = None,
    ) -> str:
        url = "codex://new?" + urlencode(
            {"path": str(project_path.expanduser().resolve()), "prompt": prompt}
        )
        self.launcher.open(url)
        submitted = self._auto_submit_if_enabled(
            result,
            expected_prompt=prompt,
            reopen_callback=lambda: self.launcher.open(url),
            execution_preferences=execution_preferences,
        )
        if submitted is False:
            return ""
        return url

    def _auto_submit_if_enabled(
        self,
        result: ProcessResult | None = None,
        expected_prompt: str = "",
        reopen_callback: Callable[[], None] | None = None,
        execution_preferences: dict[str, str] | None = None,
    ) -> bool | None:
        if not getattr(self.config, "auto_submit_codex_prompt", False):
            return None
        submit_args = (
            float(getattr(self.config, "codex_submit_delay_seconds", 3.0)),
            str(getattr(self.config, "codex_submit_hotkey", "enter")),
            expected_prompt,
            reopen_callback,
        )
        if execution_preferences:
            submitted = self.submitter.submit_after_delay(
                *submit_args,
                model=str(execution_preferences.get("model") or ""),
                reasoning_effort=str(
                    execution_preferences.get("reasoning_effort") or ""
                ),
            )
        else:
            submitted = self.submitter.submit_after_delay(*submit_args)
        recovery_action = str(
            getattr(self.submitter, "last_recovery_action", "") or ""
        )
        if result is not None and recovery_action == "start_codex":
            result.messages.append(
                "Hub 指令首次未送达；检测到 Codex 未运行，已按需启动并重试。"
            )
        if submitted is True:
            if result is not None:
                result.messages.append("Codex 提示已自动发送。")
            return True
        detail = str(getattr(self.submitter, "last_error", "") or "未知原因")
        if result is not None:
            result.messages.append(f"Codex 自动发送失败：{detail}")
        return False

    def _verified_visible_submission_required(self) -> bool:
        return bool(
            getattr(self.config, "open_codex", False)
            and getattr(self.config, "auto_submit_codex_prompt", False)
            and not getattr(self.config, "auto_execute_codex", False)
        )

    def _build_codex_exec_prompt(self, prompt_path: Path, prompt_body: str) -> str:
        return "\n".join(
            [
                "# Codex Hub 自动执行",
                "",
                "请直接执行下面的 Hub 原文内容。只处理这一项，不要扩展到无关任务。",
                "除非原文明确要求，不要触碰 SW /upload 或 /u。",
                "完成后输出简洁结果摘要；若原文要求结构化复盘，请按格式追加。CodexBridge 会负责回写 Hub。",
                "",
                "## Hub 原文",
                prompt_body,
                "",
                "## 本地文件兜底",
                f"Prompt file: {prompt_path}",
            ]
        )

    def _build_codex_launch_prompt(self, prompt_path: Path, prompt_body: str) -> str:
        full_prompt = "\n".join(
            [
                "# Codex Hub Item",
                "",
                "请直接执行下面的 Hub 原文内容。只处理这一项，不要扩展到无关任务。",
                "除非原文明确要求，不要触碰 SW /upload 或 /u。",
                "Windows UTF-8 安全：不要把中文从 Windows PowerShell 5.1 管道传给 Python；回写时请把完整回复保存为 UTF-8 文件，并优先使用 codex-hub-reply --body-file。",
                "",
                "## Hub 原文",
                prompt_body,
                "",
                "## 本地文件兜底",
                f"Prompt file: {prompt_path}",
                "",
                "完成后，请把结果简洁写回 Codex Hub 对应项目聊天或任务。",
            ]
        )
        # Windows protocol activation and Electron's URL handling are bounded
        # in encoded characters, not Chinese Unicode characters. Keep long
        # messages in their durable UTF-8 file instead of risking a partial draft.
        if (len(full_prompt) <= MAX_INLINE_CODEX_PROMPT_CHARS
                and len(urlencode({"prompt": full_prompt})) <= MAX_ENCODED_CODEX_PROMPT_CHARS):
            return full_prompt
        return "\n".join(
            [
                "# Codex Hub Item",
                "",
                "Hub 原文较长，已保存到本地文件。请读取并执行这个文件内容。",
                f"Prompt file: {prompt_path}",
                "",
                "除非文件明确要求，不要触碰 SW /upload 或 /u。",
                "Windows UTF-8 安全：不要把中文从 Windows PowerShell 5.1 管道传给 Python；回写时请把完整回复保存为 UTF-8 文件，并优先使用 codex-hub-reply --body-file。",
                "完成后，请把结果简洁写回 Codex Hub 对应项目聊天或任务。",
            ]
        )

    def _write_prompt_file(self, prefix: str, body: str) -> Path:
        self.config.prompt_dir.mkdir(parents=True, exist_ok=True)
        safe = safe_filename(prefix)
        path = self.config.prompt_dir / f"{safe}.md"
        counter = 2
        while path.exists():
            path = self.config.prompt_dir / f"{safe}-{counter}.md"
            counter += 1
        path.write_text(body, encoding="utf-8")
        return path

    def _build_mention_prompt(
        self,
        mention: dict[str, Any],
        device_label: str,
        project_memory: str = "",
    ) -> str:
        selected = mention_delivery_mode(mention) == "selected"
        primary_device_label = "" if selected else str(mention.get("primary_device_label") or "")
        delivery_reason = str(mention.get("delivery_reason") or "mention")
        if selected:
            delivery_reason = "selected"
        collaboration_role = str(mention.get("collaboration_role") or "")
        group_chat_roles = [
            role
            for role in (mention.get("group_chat_roles") or [])
            if isinstance(role, dict) and str(role.get("name") or "").strip()
        ]
        collaboration_protocol: list[str] = []
        if delivery_reason == "collaboration" and collaboration_role == "advisor":
            collaboration_protocol = [
                "",
                "## Collaboration Role",
                "You are an advisory collaborator in primary-device collaboration mode.",
                "Analyze the request or primary plan, find omissions, risks, and weak assumptions, then propose concise improvements.",
                "Do not present yourself as the final decision maker and do not start an open-ended device-to-device loop.",
            ]
        elif delivery_reason == "collaboration" and collaboration_role == "primary":
            collaboration_protocol = [
                "",
                "## Collaboration Role",
                "You are the primary executor and final decision maker.",
                "Complete the user request, evaluate later advisory feedback independently, and accept, modify, or reject it as appropriate.",
            ]
        role_protocol: list[str] = []
        if group_chat_roles and not selected:
            role_protocol = [
                "",
                "## Assigned Group Chat Roles",
                *[
                    f"- @{role['name']}: {str(role.get('instructions') or 'Fulfill this named role for the request.')}"
                    for role in group_chat_roles
                ],
                "When a role is explicitly mentioned, answer from that role's responsibility. In group-chat delivery, incorporate every assigned role that is relevant.",
            ]
        memory_protocol: list[str] = []
        if project_memory.strip():
            memory_protocol = [
                "",
                "## Durable Project Memory",
                project_memory.strip(),
                "",
                "Use this project memory as shared cross-conversation and cross-device context. The current Hub message still defines the immediate scope.",
            ]
        return "\n".join(
            [
                "# Codex Hub Collaboration" if delivery_reason == "collaboration" else "# Codex Hub Mention",
                *memory_protocol,
                "",
                f"Target device: {device_label}",
                f"Primary device: @{primary_device_label}" if primary_device_label else "Primary device: (not set)",
                f"Delivery reason: {delivery_reason}",
                f"Project: {mention.get('project_name')} ({mention.get('project_slug')})",
                f"Entry: #{mention.get('id')}",
                f"Delivery mode: {mention_delivery_mode(mention)}",
                f"From: {mention.get('source_label')}",
                "",
                "## Message",
                str(mention.get("body_markdown") or ""),
                *collaboration_protocol,
                *role_protocol,
                "",
                "## Required Return",
                "If action is needed, complete it and write a concise result back to the Hub project chat.",
                f"If you are not @{primary_device_label}, include @{primary_device_label} in your Hub reply so the primary device is notified." if primary_device_label else "No primary device is set for this project; reply normally.",
                ("Execute independently as an explicitly selected device. Do not summon or @ other devices, assign advisory roles, or initiate group discussion."
                 if selected else "Other AI/device suggestions are advisory only. The primary Codex device decides whether to adopt them."),
                f"When using codex-hub-reply, pass --mention-id {mention.get('id')} so the reply is linked to this request and does not start another group round.",
                (f"Reply in the original task conversation by also passing --task-id {mention.get('task_id')}."
                 if mention.get("task_id") is not None else "Reply in the project conversation; do not supply --task-id."),
                "If durable project facts, decisions, known issues, key files, or next steps change, include a concise memory or handoff update in the Hub result.",
            ]
        )

    def _prepare_project_memory(
        self,
        project_slug: str,
        *,
        device_label: str,
        result: ProcessResult,
    ) -> str:
        getter = getattr(self.client, "get_project_memory_markdown", None)
        if not callable(getter):
            return ""
        try:
            memory = validate_project_memory_document(
                str(getter(project_slug) or "")
            )
        except Exception as exc:
            fallback = self._load_valid_local_project_memory(project_slug)
            if fallback:
                result.messages.append(
                    f"远端项目记忆未通过安全检查，已使用本地正常副本：{exc}"
                )
                return fallback
            result.messages.append(f"项目记忆未注入：{exc}")
            return ""
        if not memory:
            return ""
        runtime_memory = self._prepare_runtime_project_memory(
            project_slug, fallback=memory, result=result
        )
        digest = hashlib.sha256(memory.encode("utf-8")).hexdigest()
        project_path = self._resolve_project_path(project_slug)
        if project_path is None:
            result.messages.append("项目记忆已读取，但本地项目目录不可用。")
            return runtime_memory
        memory_dir = project_path / ".codex-hub"
        memory_dir.mkdir(parents=True, exist_ok=True)
        memory_path = memory_dir / "PROJECT_MEMORY.md"
        self._write_text_atomic(memory_path, memory + "\n")
        if self._memory_sync_cache.get(project_slug) == digest:
            return runtime_memory
        self._memory_sync_cache[project_slug] = digest
        manifest = self._build_project_manifest(project_path)
        sync_path = memory_dir / "sync.json"
        version_match = MEMORY_VERSION_RE.search(memory)
        memory_version = int(version_match.group(1)) if version_match else 0
        sync_payload = {
            "project_slug": project_slug,
            "device_label": device_label,
            "memory_version": memory_version,
            "memory_sha256": digest,
            "git_commit": manifest.get("git_commit") or "",
            "manifest": manifest,
        }
        self._write_text_atomic(
            sync_path,
            json.dumps(sync_payload, ensure_ascii=False, indent=2) + "\n",
        )
        poster = getattr(self.client, "post_project_sync_snapshot", None)
        if callable(poster):
            try:
                poster(
                    project_slug,
                    memory_version=memory_version,
                    git_commit=str(manifest.get("git_commit") or ""),
                    manifest=manifest,
                )
            except Exception as exc:
                result.messages.append(f"项目差异清单暂未上传：{exc}")
        return runtime_memory

    def _prepare_runtime_project_memory(
        self,
        project_slug: str,
        *,
        fallback: str,
        result: ProcessResult,
    ) -> str:
        runtime_getter = getattr(self.client, "get_project_memory_runtime_markdown", None)
        if callable(runtime_getter):
            try:
                runtime_memory = validate_project_memory_document(
                    str(runtime_getter(project_slug) or "")
                )
                if runtime_memory:
                    return runtime_memory
            except Exception as exc:
                result.messages.append(
                    f"精简项目记忆暂不可用，已使用完整版本：{exc}"
                )
        return fallback

    def _load_valid_local_project_memory(self, project_slug: str) -> str:
        project_path = self._resolve_project_path(project_slug)
        if project_path is None:
            return ""
        memory_path = project_path / ".codex-hub" / "PROJECT_MEMORY.md"
        try:
            return validate_project_memory_document(
                memory_path.read_text(encoding="utf-8-sig")
            )
        except (OSError, UnicodeError, ValueError):
            return ""

    def _build_project_manifest(self, project_path: Path) -> dict[str, Any]:
        files: list[dict[str, Any]] = []
        root = project_path.resolve()
        for current_root, dir_names, file_names in os.walk(root):
            dir_names[:] = sorted(
                name
                for name in dir_names
                if name not in MANIFEST_IGNORED_DIRS
            )
            for file_name in sorted(file_names):
                if len(files) >= MAX_MANIFEST_FILES:
                    break
                if file_name in MANIFEST_IGNORED_FILES or file_name.endswith(
                    (".tmp", ".part", ".pyc")
                ):
                    continue
                path = Path(current_root) / file_name
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    size = path.stat().st_size
                    sha256 = (
                        self._sha256_file(path)
                        if size <= MAX_AUTOMATIC_HASH_BYTES
                        else ""
                    )
                    relative = path.relative_to(root).as_posix()
                except (OSError, ValueError):
                    continue
                files.append(
                    {
                        "path": relative,
                        "size": size,
                        "sha256": sha256,
                        "kind": "memory" if relative.startswith(".codex-hub/") else "file",
                        "required": relative == ".codex-hub/PROJECT_MEMORY.md",
                    }
                )
            if len(files) >= MAX_MANIFEST_FILES:
                break
        return {
            "schema_version": 1,
            "git_commit": self._git_commit(root),
            "files": files,
        }

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _git_commit(project_path: Path) -> str:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
        try:
            completed = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                creationflags=flags,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return completed.stdout.strip() if completed.returncode == 0 else ""

    @staticmethod
    def _write_text_atomic(path: Path, body: str) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(body, encoding="utf-8")
        os.replace(temporary, path)

    def _seed_existing(
        self, tasks: list[dict[str, Any]], mentions: list[dict[str, Any]]
    ) -> None:
        for task in tasks:
            task_id = str(task["id"])
            self.state.seen_task_ids.add(task_id)
            self.state.prompted_task_ids.add(task_id)
        for mention in mentions:
            mention_id = str(mention["id"])
            self.state.seen_mention_ids.add(mention_id)
            self.state.prompted_mention_ids.add(mention_id)
            self.state.acked_mention_ids.add(mention_id)
        self.state.initialized = True

    def _save_state(self) -> None:
        self.state.save(self.config.state_path)


def mention_delivery_mode(mention: dict[str, Any]) -> str:
    metadata = mention.get("metadata") or mention.get("metadata_json") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = {}
    routing = metadata.get("routing") if isinstance(metadata, dict) else {}
    if not isinstance(routing, dict):
        routing = {}
    return str(mention.get("delivery_mode") or routing.get("mode") or "group")


def apply_device_mention_policy(
    body: str,
    *,
    primary_device_label: str = "",
    current_device_label: str = "",
) -> str:
    """Notify the primary device only when another device is replying.

    The primary device never mentions itself. A secondary AI/device mentions the
    configured primary exactly once so its result wakes the decision-making
    device. Explicit cross-device handoffs remain the author's responsibility;
    the bridge does not infer targets from ordinary prose.
    """
    primary = primary_device_label.strip().lstrip("@")
    current = current_device_label.strip().lstrip("@")
    if not primary or primary.casefold() == current.casefold():
        return body
    if contains_exact_device_mention(body, primary):
        return body
    return f"@{primary}\n\n{body}"


def contains_exact_device_mention(body: str, device_label: str) -> bool:
    label = device_label.strip().lstrip("@")
    if not label:
        return False
    pattern = rf"(?<![A-Za-z0-9_.-])@{re.escape(label)}(?![A-Za-z0-9_.-])"
    return re.search(pattern, body, flags=re.IGNORECASE) is not None


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return cleaned.lower() or "item"


def trim_text(value: str, limit: int) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n\n[内容过长，已截断]"
