"""Independent agent-only worker; never consumes legacy inbox work or mentions."""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import sys
import threading
import time
from pathlib import Path
from dataclasses import replace
from typing import Any, Callable

from .client import HubClient, HubClientError
from .config import load_config
from .runner import BridgeRunner
from .single_instance import SingleInstance


def filter_agent_inbox(inbox: dict[str, Any], pending_ids: set[int] | None = None) -> dict[str, Any]:
    device_id = (inbox.get("device") or {}).get("id")
    if type(device_id) is not int or device_id < 1:
        raise HubClientError("Hub 未返回有效的本连接身份，已停止领取任务。")
    pending = pending_ids or set()
    tasks = []
    for task in inbox.get("tasks") or []:
        agent_id = task.get("assigned_agent_id")
        if (type(agent_id) is not int or agent_id < 1
                or task.get("assigned_device_id") != device_id):
            continue
        # Existing work without a local recovery receipt is not a new request.
        # A fresh worker must not repeat an in-progress or already reviewed job.
        if task.get("status") in {"open", "claimed"} or task.get("id") in pending:
            tasks.append(task)
    return {**inbox, "tasks": tasks, "mentions": []}


class AgentOnlyHubClient(HubClient):
    pending_task_ids: Callable[[], set[int]] = staticmethod(set)
    machine_name: str = ''
    lane: str = ''
    task_selector = None
    status_callback = None

    def get_inbox(self) -> dict[str, Any]:
        inbox=filter_agent_inbox(super().get_inbox(), self.pending_task_ids())
        if self.status_callback:
            self.status_callback({'connected': True, 'device': inbox['device']})
        if self.lane:
            inbox['tasks']=[t for t in inbox['tasks'] if (t.get('assigned_agent_kind') or 'worker')==self.lane]
        if self.task_selector is not None:
            inbox['tasks']=self.task_selector(inbox['tasks'])
        return inbox

    def post_device_capabilities(self, capabilities: dict[str, Any]) -> dict[str, Any]:
        return super().post_device_capabilities({**capabilities,
            'machine_name': self.machine_name or platform.node(), 'runtime_kind': 'durable-agent'})


def controller_config(config):
    root=config.state_path.parent/'controller-workspaces'
    return replace(config,state_path=config.state_path.with_name('controller-'+config.state_path.name),
        prompt_dir=config.prompt_dir/'controller',codex_exec_output_dir=config.codex_exec_output_dir/'controller',
        projects={slug:root/slug for slug in config.projects},project_root=root,auto_create_missing_projects=True,
        interval_seconds=3,codex_exec_sandbox='workspace-write',
        backend_options={**config.backend_options,'agent_network_access':True})


def run_lane(runner, stopped, interval, emit=None):
    emit = emit or (lambda message: print(message, flush=True))
    while not stopped.is_set():
        try:
            result=runner.process_once()
            for message in result.messages: emit(message)
        except Exception as exc:
            emit(f'执行连接暂未完成：{type(exc).__name__}；保留回执等待恢复。')
        stopped.wait(max(3,interval))

class AgentWorkerHeartbeat:
    """Keep a busy dedicated worker visible without polling or claiming work."""
    def __init__(self, client: HubClient, interval_seconds: float = 45, on_success=None):
        self.client = client
        self.interval_seconds = max(0.01, interval_seconds)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name='hub-agent-heartbeat', daemon=True)
        self.on_success = on_success

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stopped.set()
        if self.thread.is_alive():
            self.thread.join(timeout=max(3, self.client.timeout_seconds + 1))

    def _run(self) -> None:
        while not self.stopped.is_set():
            try:
                self.client.heartbeat()
                if self.on_success:
                    self.on_success({'connected': True})
            except Exception:
                # Never print server bodies/credentials, reset receipts, or
                # trigger another execution when only a heartbeat fails.
                pass
            self.stopped.wait(self.interval_seconds)


def mutex_name(config_path: Path, binding_id: str) -> str:
    identity = os.path.normcase(str(config_path.resolve())) + "|" + binding_id
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return "Local\\CodexHub.AgentWorker." + digest


def main(argv: list[str] | None = None, *, stop_event=None, status_callback=None, log_callback=None) -> int:
    emit = log_callback or (lambda message: print(message, flush=True))
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTHONUTF8"] = "1"
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Run only durable-agent tasks on a dedicated Hub connection.")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--binding-id")
    parser.add_argument("--once", action="store_true", help="Report capabilities and inbox counts once; never claim or execute work.")
    args = parser.parse_args(argv)
    try:
        config_path = args.config.expanduser().resolve(strict=True)
        runtimes = load_config(config_path).runtime_configs()
        if args.binding_id:
            runtimes = [item for item in runtimes if item.binding_id == args.binding_id]
        if len(runtimes) != 1:
            raise ValueError("专用 worker 必须选择一个配置完整的执行连接。")
        config = runtimes[0]
        if config.execution_backend != "codex" or not config.auto_execute_codex:
            raise ValueError("专用 worker 需要启用 Codex CLI 自动执行。")
        if config.ignore_existing_on_first_run:
            raise ValueError("专用 worker 配置需设置 ignore_existing_on_first_run=false，避免跳过已分配工作。")
        # The new process supplies a distinct config and state directory; this
        # mutex intentionally never touches the old Bridge singleton.
        instance = SingleInstance(mutex_name(config_path, config.binding_id))
        if not instance.acquire():
            emit("该专用智能体 worker 已在运行。")
            if status_callback: status_callback({'external': True})
            return 0
        try:
            os.environ["CODEX_BRIDGE_AGENT_CONFIG"] = str(config_path)
            package_root = str(Path(__file__).resolve().parent.parent)
            current_pythonpath = os.environ.get("PYTHONPATH", "")
            paths = [package_root, *[p for p in current_pythonpath.split(os.pathsep) if p and p != package_root]]
            os.environ["PYTHONPATH"] = os.pathsep.join(paths)
            client = AgentOnlyHubClient(config.hub, config.token,
                timeout_seconds=config.hub_request_timeout_seconds,
                get_retry_attempts=config.hub_get_retry_attempts,
                retry_backoff_seconds=config.hub_retry_backoff_seconds,
                source_ip=config.hub_source_ip)
            client.machine_name = str(config.backend_options.get('machine_name') or '').strip()[:128]
            client.status_callback = status_callback
            runner = BridgeRunner(config, client=client)
            client.pending_task_ids = lambda: {int(item["task_id"]) for item in runner.state.task_deliveries.values()
                                               if isinstance(item.get("task_id"), int)}
            if args.once:
                status = runner.poll_status()
                emit(f"连接已检查：{status.device_label}；可处理智能体任务 {status.task_count} 项；未领取或执行。")
                return 0
            heartbeat = AgentWorkerHeartbeat(HubClient(config.hub, config.token,
                timeout_seconds=min(12, config.hub_request_timeout_seconds),
                get_retry_attempts=1, source_ip=config.hub_source_ip), on_success=status_callback)
            controller=controller_config(config)
            controller_client=AgentOnlyHubClient(config.hub,config.token,timeout_seconds=config.hub_request_timeout_seconds,
                get_retry_attempts=config.hub_get_retry_attempts,retry_backoff_seconds=config.hub_retry_backoff_seconds,source_ip=config.hub_source_ip)
            controller_client.machine_name=client.machine_name
            controller_client.status_callback=status_callback
            controller_client.lane='controller'; client.lane='worker'
            controller_runner=BridgeRunner(controller,client=controller_client)
            controller_client.pending_task_ids=lambda: {int(item['task_id']) for item in controller_runner.state.task_deliveries.values() if isinstance(item.get('task_id'),int)}
            # The caller's event means intentional shutdown. Never set it on
            # failure, otherwise the desktop supervisor cannot restart us.
            stopped=threading.Event()
            controller_thread=threading.Thread(target=run_lane,args=(controller_runner,stopped,3,emit),name='hub-controller',daemon=True)
            # Preserve and recover pre-pool receipts without letting the legacy
            # state file consume new work. Each new task owns its own durable state.
            client.task_selector=lambda tasks:[task for task in tasks if task['id'] in client.pending_task_ids()]
            recovery_thread=threading.Thread(target=run_lane,args=(runner,stopped,12,emit),name='hub-legacy-recovery',daemon=True)
            pool_client=AgentOnlyHubClient(config.hub,config.token,timeout_seconds=config.hub_request_timeout_seconds,
                get_retry_attempts=config.hub_get_retry_attempts,retry_backoff_seconds=config.hub_retry_backoff_seconds,source_ip=config.hub_source_ip)
            pool_client.lane='worker'
            pool_client.status_callback=status_callback
            pool_client.task_selector=lambda tasks:[task for task in tasks if task['id'] not in client.pending_task_ids()]
            from .task_pool import DurableTaskPool
            pool=DurableTaskPool(config,pool_client,capacity=config.backend_options.get('agent_parallelism',2))
            try:
                if stop_event is not None and stop_event.is_set():
                    return 0
                heartbeat.start()
                controller_thread.start()
                recovery_thread.start()
                emit(f"专用智能体 worker 已启动；独立任务最多并行 {pool.capacity} 项，共享工程顺序执行，总控保持独立。")
                while not stopped.is_set() and not (stop_event and stop_event.is_set()):
                    if not all(thread.is_alive() for thread in (controller_thread, recovery_thread, heartbeat.thread)):
                        raise RuntimeError('An agent service lane exited unexpectedly')
                    try:
                        for message in pool.tick(): emit(message)
                    except Exception as exc:
                        # Durable receipts distinguish safe result retries
                        # from uncertain work; do not reset that state.
                        emit(f"本轮未完成：{type(exc).__name__}；保留回执等待恢复。")
                    (stop_event if stop_event is not None else stopped).wait(3)
            finally:
                stopped.set()
                if status_callback: status_callback({'phase': 'stopping'})
                try:
                    try:
                        pool.close()
                    finally:
                        if controller_thread.ident is not None: controller_thread.join()
                        if recovery_thread.ident is not None: recovery_thread.join()
                finally:
                    heartbeat.close()
                emit('Hub 智能体后台已完成收尾并停止。')
        finally:
            instance.close()
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, HubClientError) as exc:
        parser.exit(1, f"Agent worker: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
