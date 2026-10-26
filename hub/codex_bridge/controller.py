from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from .client import HubClientError
from .i18n import text
from .network_guard import NetworkGuard
from .runner import BridgeRunner, ProcessResult


LogCallback = Callable[[str], None]
RunnerFactory = Callable[[Any], BridgeRunner]
BUSY_DISPLAY_DELAY_SECONDS = 0.5


class BridgeController:
    """Run every AI binding with an independent heartbeat and work loop."""

    def __init__(
        self,
        config: Any,
        runner_factory: RunnerFactory | None = None,
        log_callback: LogCallback | None = None,
        network_guard: Any | None = None,
    ):
        self.config = config
        self.runner_factory = runner_factory or BridgeRunner
        self.log_callback = log_callback or (lambda message: None)
        runtime_configs = self._runtime_configs(config)
        self.binding_configs: dict[str, Any] = {}
        self.runners: dict[str, BridgeRunner] = {}
        for index, runtime_config in enumerate(runtime_configs):
            binding_id = str(
                getattr(runtime_config, "binding_id", "") or f"binding-{index + 1}"
            )
            self.binding_configs[binding_id] = runtime_config
            self.runners[binding_id] = self.runner_factory(runtime_config)

        self.runner = next(iter(self.runners.values()), None)
        self.network_guard = network_guard or NetworkGuard(config)
        self.latest_result: ProcessResult | None = None
        self.latest_error = ""
        self.latest_results: dict[str, ProcessResult] = {}
        self.latest_errors: dict[str, str] = {}
        self.latest_worker_errors: dict[str, str] = {}
        self.busy_bindings: set[str] = set()
        self.worker_started_at: dict[str, float] = {}
        self.status_text = text("stopped")
        self._stop_event = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self._heartbeat_threads: dict[str, threading.Thread] = {}
        self._worker_threads: dict[str, threading.Thread] = {}
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._network_guard_lock = threading.Lock()

    @staticmethod
    def _runtime_configs(config: Any) -> list[Any]:
        expand = getattr(config, "runtime_configs", None)
        if callable(expand):
            return list(expand())
        return [config]

    @property
    def is_running(self) -> bool:
        return any(thread.is_alive() for thread in self._threads.values())

    def start(self) -> None:
        if self.is_running:
            return
        if not self.runners:
            self.status_text = text("missing_token")
            self.log(text("missing_token"))
            return
        self._stop_event.clear()
        self._threads = {}
        self._heartbeat_threads = {}
        self._worker_threads = {}
        for binding_id, runner in self.runners.items():
            if callable(getattr(runner, "poll_status", None)):
                heartbeat = threading.Thread(
                    target=self._heartbeat_loop,
                    args=(binding_id,),
                    name=f"CodexBridge-{binding_id}-heartbeat",
                    daemon=True,
                )
                worker = threading.Thread(
                    target=self._worker_loop,
                    args=(binding_id,),
                    name=f"CodexBridge-{binding_id}-worker",
                    daemon=True,
                )
                self._heartbeat_threads[binding_id] = heartbeat
                self._worker_threads[binding_id] = worker
                self._threads[f"{binding_id}:heartbeat"] = heartbeat
                self._threads[f"{binding_id}:worker"] = worker
                heartbeat.start()
                worker.start()
            else:
                thread = threading.Thread(
                    target=self._legacy_loop,
                    args=(binding_id,),
                    name=f"CodexBridge-{binding_id}",
                    daemon=True,
                )
                self._threads[binding_id] = thread
                thread.start()
        self._thread = next(iter(self._threads.values()), None)
        self.log(f"{text('bridge_started')} AI 绑定={len(self.runners)}")
        if any(
            str(getattr(config, "execution_backend", "")).lower() == "codex"
            for config in self.binding_configs.values()
        ):
            self.log(text("codex_delivery_recovery_mode"))

    def stop(self) -> None:
        self._stop_event.set()
        deadline = time.monotonic() + 5
        for thread in list(self._threads.values()):
            remaining = max(0.0, deadline - time.monotonic())
            if thread.is_alive() and remaining:
                thread.join(timeout=remaining)
        self.status_text = text("stopped")
        self.log(text("bridge_stopped"))

    def run_once(self) -> ProcessResult:
        if not self.runners:
            raise RuntimeError(text("missing_token"))
        first_result: ProcessResult | None = None
        for binding_id in self.runners:
            result = self._run_binding(binding_id)
            first_result = first_result or result
        assert first_result is not None
        return first_result

    def run_once_safely(self) -> ProcessResult | None:
        results = self.run_all_once_safely()
        return next(iter(results.values()), None)

    def run_all_once_safely(self) -> dict[str, ProcessResult]:
        results: dict[str, ProcessResult] = {}
        if not self.runners:
            self.latest_error = text("missing_token")
            self.status_text = text("missing_token")
            return results
        for binding_id in self.runners:
            result = self._run_binding_safely(binding_id)
            if result is not None:
                results[binding_id] = result
        return results

    def binding_snapshots(self) -> list[dict[str, Any]]:
        with self._lock:
            rows: list[dict[str, Any]] = []
            now = time.monotonic()
            for binding_id, config in self.binding_configs.items():
                runner = self.runners[binding_id]
                pending_reader = getattr(runner, "pending_mention_deliveries", None)
                pending_mentions = (
                    pending_reader() if callable(pending_reader) else []
                )
                task_reader = getattr(runner, "pending_task_deliveries", None)
                pending_tasks = task_reader() if callable(task_reader) else []
                pending = [*pending_tasks, *pending_mentions]
                result = self.latest_results.get(binding_id)
                error = self.latest_errors.get(binding_id, "")
                worker_error = self.latest_worker_errors.get(binding_id, "")
                thread = self._heartbeat_threads.get(binding_id) or self._threads.get(
                    binding_id
                )
                busy = (
                    binding_id in self.busy_bindings
                    and now - self.worker_started_at.get(binding_id, now)
                    >= BUSY_DISPLAY_DELAY_SECONDS
                )
                backend_name = str(
                    getattr(config, "binding_name", "")
                    or getattr(config, "execution_backend", "AI").title()
                )
                if error:
                    status = f"连接异常：{error}"
                elif result and busy:
                    status = f"在线 · {backend_name} 执行中"
                elif result and worker_error:
                    status = f"在线 · 执行异常：{worker_error}"
                elif pending:
                    status = f"在线 · {len(pending)} 项待确认"
                elif result:
                    status = "在线"
                elif thread and thread.is_alive():
                    status = "连接中"
                else:
                    status = "已停止"
                rows.append(
                    {
                        "id": binding_id,
                        "name": getattr(config, "binding_name", "") or binding_id,
                        "backend": getattr(config, "execution_backend", "codex"),
                        "device": result.device_label if result else "-",
                        "status": status,
                        "pending_count": len(pending),
                    }
                )
            return rows

    def pending_mention_deliveries(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for binding_id, runner in self.runners.items():
            config = self.binding_configs[binding_id]
            pending_reader = getattr(runner, "pending_mention_deliveries", None)
            deliveries = pending_reader() if callable(pending_reader) else []
            for delivery in deliveries:
                row = dict(delivery)
                row["binding_id"] = binding_id
                row["binding_name"] = str(
                    getattr(config, "binding_name", "") or binding_id
                )
                rows.append(row)
        return rows

    def pending_deliveries(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for binding_id, runner in self.runners.items():
            config = self.binding_configs[binding_id]
            binding_name = str(
                getattr(config, "binding_name", "") or binding_id
            )
            for item_type, reader_name in (
                ("task", "pending_task_deliveries"),
                ("mention", "pending_mention_deliveries"),
            ):
                reader = getattr(runner, reader_name, None)
                deliveries = reader() if callable(reader) else []
                for delivery in deliveries:
                    row = dict(delivery)
                    row["item_type"] = item_type
                    row["binding_id"] = binding_id
                    row["binding_name"] = binding_name
                    rows.append(row)
        return rows

    def confirm_pending_mention(
        self, binding_id: str, project_slug: str, mention_id: int
    ) -> None:
        self.runners[binding_id].confirm_pending_mention(project_slug, mention_id)
        self.log(
            f"[{self._binding_name(binding_id)}] 已确认提及 #{mention_id} 已发送，并完成 Hub 回执。"
        )

    def retry_pending_mention(
        self, binding_id: str, project_slug: str, mention_id: int
    ) -> None:
        self.runners[binding_id].retry_pending_mention(project_slug, mention_id)
        self.log(
            f"[{self._binding_name(binding_id)}] 已解除提及 #{mention_id} 的防重复锁；下一轮将重新投递。"
        )

    def confirm_pending_task(
        self, binding_id: str, project_slug: str, task_id: int
    ) -> None:
        self.runners[binding_id].confirm_pending_task(project_slug, task_id)
        self.log(
            f"[{self._binding_name(binding_id)}] 已确认任务 #{task_id} 已发送。"
        )

    def retry_pending_task(
        self, binding_id: str, project_slug: str, task_id: int
    ) -> None:
        self.runners[binding_id].retry_pending_task(project_slug, task_id)
        self.log(
            f"[{self._binding_name(binding_id)}] 已解除任务 #{task_id} 的防重复锁；"
            "下一轮将重新投递。"
        )

    def _run_binding(self, binding_id: str) -> ProcessResult:
        result = self.runners[binding_id].process_once()
        with self._lock:
            self.latest_results[binding_id] = result
            self.latest_worker_errors.pop(binding_id, None)
            self.latest_result = result
            self.latest_error = next(iter(self.latest_errors.values()), "")
            self._update_status_locked()
        self._log_result(binding_id, result)
        return result

    def _run_binding_safely(
        self, binding_id: str, *, worker: bool = False
    ) -> ProcessResult | None:
        try:
            return self._run_binding(binding_id)
        except HubClientError as exc:
            if worker and self._has_independent_heartbeat(binding_id):
                self._record_worker_error(binding_id, str(exc))
                self.log(f"[{self._binding_name(binding_id)}] 执行轮询失败：{exc}")
            else:
                changed = self._record_error(
                    binding_id, str(exc), text("hub_connection_error")
                )
                if changed:
                    self.log(f"[{self._binding_name(binding_id)}] {exc}")
                self._check_network_guard()
            return None
        except Exception as exc:  # defensive UI boundary
            if worker and self._has_independent_heartbeat(binding_id):
                self._record_worker_error(binding_id, str(exc))
                self.log(
                    f"[{self._binding_name(binding_id)}] "
                    f"{text('unexpected_bridge_error')}: {exc}"
                )
            else:
                self._record_error(binding_id, str(exc), text("bridge_error"))
                self.log(
                    f"[{self._binding_name(binding_id)}] "
                    f"{text('unexpected_bridge_error')}: {exc}"
                )
            return None

    def _heartbeat_binding_safely(self, binding_id: str) -> ProcessResult | None:
        runner = self.runners[binding_id]
        try:
            result = runner.poll_status()
        except HubClientError as exc:
            changed = self._record_error(
                binding_id, str(exc), text("hub_connection_error")
            )
            if changed:
                self.log(f"[{self._binding_name(binding_id)}] 心跳失败：{exc}")
            self._check_network_guard()
            return None
        except Exception as exc:
            changed = self._record_error(binding_id, str(exc), text("bridge_error"))
            if changed:
                self.log(f"[{self._binding_name(binding_id)}] 心跳异常：{exc}")
            return None

        with self._lock:
            recovered = (
                binding_id not in self.latest_results
                or binding_id in self.latest_errors
            )
            self.latest_results[binding_id] = result
            self.latest_errors.pop(binding_id, None)
            self.latest_result = result
            self.latest_error = next(iter(self.latest_errors.values()), "")
            self._update_status_locked()
        if recovered:
            self.log(
                f"[{self._binding_name(binding_id)}] Hub 心跳在线："
                f"{result.device_label or 'device'}"
            )
        return result

    def _record_error(
        self, binding_id: str, error: str, single_status: str
    ) -> bool:
        with self._lock:
            changed = self.latest_errors.get(binding_id) != error
            self.latest_errors[binding_id] = error
            self.latest_error = error
            if len(self.runners) == 1:
                self.status_text = single_status
            else:
                self._update_status_locked()
            return changed

    def _record_worker_error(self, binding_id: str, error: str) -> None:
        with self._lock:
            self.latest_worker_errors[binding_id] = error
            self._update_status_locked()

    def _update_status_locked(self) -> None:
        total = len(self.runners)
        if total == 1:
            binding_id = next(iter(self.runners), "")
            if binding_id in self.latest_errors:
                self.status_text = text("bridge_error")
                return
            result = self.latest_results.get(binding_id)
            if result:
                self.status_text = (
                    f"{result.device_label or 'device'} 在线，"
                    f"任务={result.task_count}，提及={result.mention_count}"
                )
            return
        online = sum(
            1
            for binding_id in self.runners
            if binding_id in self.latest_results
            and binding_id not in self.latest_errors
        )
        errors = len(self.latest_errors)
        self.status_text = f"{online}/{total} 个 AI 在线"
        if errors:
            self.status_text += f"，{errors} 个连接异常"

    def _legacy_loop(self, binding_id: str) -> None:
        config = self.binding_configs[binding_id]
        while not self._stop_event.is_set():
            self._run_binding_safely(binding_id)
            self._stop_event.wait(self._interval(config))

    def _heartbeat_loop(self, binding_id: str) -> None:
        config = self.binding_configs[binding_id]
        while not self._stop_event.is_set():
            self._heartbeat_binding_safely(binding_id)
            self._stop_event.wait(self._interval(config))

    def _worker_loop(self, binding_id: str) -> None:
        config = self.binding_configs[binding_id]
        while not self._stop_event.is_set():
            with self._lock:
                self.busy_bindings.add(binding_id)
                self.worker_started_at[binding_id] = time.monotonic()
            try:
                self._run_binding_safely(binding_id, worker=True)
            finally:
                with self._lock:
                    self.busy_bindings.discard(binding_id)
                    self.worker_started_at.pop(binding_id, None)
            self._stop_event.wait(self._interval(config))

    def _has_independent_heartbeat(self, binding_id: str) -> bool:
        return binding_id in self._heartbeat_threads

    @staticmethod
    def _interval(config: Any) -> int:
        return max(1, int(getattr(config, "interval_seconds", 20)))

    def _binding_name(self, binding_id: str) -> str:
        config = self.binding_configs[binding_id]
        return str(getattr(config, "binding_name", "") or binding_id)

    def _log_result(self, binding_id: str, result: ProcessResult) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        prefix = f"[{self._binding_name(binding_id)}]"
        self.log(
            f"[{stamp}] {prefix} {result.device_label or 'device'} 在线。"
            f"任务={result.task_count} 提及={result.mention_count} "
            f"已领取任务={len(result.claimed_tasks)} "
            f"已领取提及={len(result.claimed_mentions)}"
        )
        for task_id in result.claimed_tasks:
            self.log(f"{prefix} {text('claimed_task')} #{task_id}")
        for mention_id in result.claimed_mentions:
            self.log(f"{prefix} {text('claimed_mention')} #{mention_id}")
        for prompt_path in result.prompt_files:
            self.log(f"{prefix} {text('prompt_file')}: {prompt_path}")
        for _ in result.opened_codex:
            self.log(f"{prefix} {text('opened_codex')}")
        for exec_result in result.executed_codex:
            label = text("executed_codex") if exec_result.ok else text("codex_exec_failed")
            self.log(f"{prefix} {label}: {exec_result.summary}")
        for message in result.messages:
            self.log(f"{prefix} {message}")

    def _check_network_guard(self) -> None:
        if not self.network_guard:
            return
        if not self._network_guard_lock.acquire(blocking=False):
            return
        try:
            result = self.network_guard.check()
        except Exception as exc:
            self.log(f"{text('network_guard_failed')}: {exc}")
        else:
            self.log(result.message)
        finally:
            self._network_guard_lock.release()

    def log(self, message: str) -> None:
        self.log_callback(message)
