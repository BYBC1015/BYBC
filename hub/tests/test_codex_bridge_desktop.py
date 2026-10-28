import threading
import time
from dataclasses import dataclass, field

from codex_bridge.client import HubClientError
from codex_bridge.controller import BridgeController
from codex_bridge.runner import ProcessResult
from codex_bridge.startup import StartupManager


@dataclass
class FakeConfig:
    interval_seconds: int = 0


@dataclass
class BindingRuntimeConfig:
    binding_id: str
    binding_name: str
    execution_backend: str
    interval_seconds: int = 1


@dataclass
class MultiConfig:
    configs: list[BindingRuntimeConfig]

    def runtime_configs(self):
        return self.configs


class FakeRunner:
    def __init__(self):
        self.calls = 0

    def process_once(self):
        self.calls += 1
        return ProcessResult(
            device_label="codex-device-a",
            task_count=1,
            mention_count=0,
            claimed_tasks=[self.calls],
        )


class FailingRunner:
    def process_once(self):
        raise HubClientError("offline")


class FakeNetworkGuard:
    def __init__(self):
        self.calls = 0

    def check(self):
        self.calls += 1
        return type(
            "NetworkGuardResult",
            (),
            {
                "message": "cmynetwork may be offline",
                "ok": False,
                "action": "notify",
            },
        )()


@dataclass
class EventLog:
    entries: list[str] = field(default_factory=list)

    def __call__(self, message: str):
        self.entries.append(message)


def test_bridge_controller_runs_once_and_records_status():
    runner = FakeRunner()
    log = EventLog()
    controller = BridgeController(
        FakeConfig(),
        runner_factory=lambda _config: runner,
        log_callback=log,
    )

    result = controller.run_once()

    assert result.device_label == "codex-device-a"
    assert runner.calls == 1
    assert controller.latest_result == result
    assert controller.status_text == "codex-device-a 在线，任务=1，提及=0"
    assert any("已领取任务 #1" in entry for entry in log.entries)


def test_stopped_controller_does_not_keep_stale_online_badge():
    controller = BridgeController(FakeConfig(), runner_factory=lambda _config: FakeRunner())
    controller.run_once()
    assert controller.binding_snapshots()[0]['status'] == '在线'
    controller.stop()
    assert controller.binding_snapshots()[0]['status'] == '已停止'


def test_bridge_controller_checks_vpn_guard_after_hub_error():
    log = EventLog()
    guard = FakeNetworkGuard()
    controller = BridgeController(
        FakeConfig(),
        runner_factory=lambda _config: FailingRunner(),
        log_callback=log,
        network_guard=guard,
    )

    controller.run_once_safely()

    assert controller.status_text == "Hub 连接错误"
    assert guard.calls == 1
    assert any("cmynetwork may be offline" in entry for entry in log.entries)


def test_bridge_controller_runs_multiple_ai_bindings_independently():
    configs = [
        BindingRuntimeConfig("codex", "Codex", "codex"),
        BindingRuntimeConfig("hermes", "Hermes", "hermes"),
    ]
    calls: list[str] = []

    class BindingRunner:
        def __init__(self, config):
            self.config = config

        def process_once(self):
            calls.append(self.config.binding_id)
            return ProcessResult(device_label=f"{self.config.binding_id}-device")

    controller = BridgeController(
        MultiConfig(configs),
        runner_factory=BindingRunner,
        network_guard=FakeNetworkGuard(),
    )

    results = controller.run_all_once_safely()

    assert set(results) == {"codex", "hermes"}
    assert calls == ["codex", "hermes"]
    assert controller.status_text == "2/2 个 AI 在线"
    assert {row["device"] for row in controller.binding_snapshots()} == {
        "codex-device",
        "hermes-device",
    }


def test_slow_ai_binding_does_not_block_other_binding_heartbeat():
    configs = [
        BindingRuntimeConfig("codex", "Codex", "codex"),
        BindingRuntimeConfig("hermes", "Hermes", "hermes"),
    ]
    release_codex = threading.Event()
    hermes_heartbeat = threading.Event()

    class ConcurrentRunner:
        def __init__(self, config):
            self.config = config

        def process_once(self):
            if self.config.binding_id == "codex":
                release_codex.wait(timeout=2)
            else:
                hermes_heartbeat.set()
            return ProcessResult(device_label=f"{self.config.binding_id}-device")

    controller = BridgeController(
        MultiConfig(configs),
        runner_factory=ConcurrentRunner,
        network_guard=FakeNetworkGuard(),
    )

    controller.start()
    try:
        assert hermes_heartbeat.wait(timeout=0.5)
    finally:
        release_codex.set()
        controller.stop()


def test_long_hermes_execution_keeps_independent_heartbeat_online():
    config = BindingRuntimeConfig("hermes", "Hermes", "hermes", interval_seconds=1)
    work_started = threading.Event()
    release_work = threading.Event()
    heartbeat_seen = threading.Event()

    class HeartbeatRunner:
        def __init__(self, _config):
            self.heartbeat_calls = 0

        def poll_status(self):
            self.heartbeat_calls += 1
            heartbeat_seen.set()
            return ProcessResult(
                device_label="hermes-device-a",
                mention_count=1,
            )

        def process_once(self):
            work_started.set()
            release_work.wait(timeout=3)
            return ProcessResult(device_label="hermes-device-a")

    runner = HeartbeatRunner(config)
    controller = BridgeController(
        MultiConfig([config]),
        runner_factory=lambda _config: runner,
        network_guard=FakeNetworkGuard(),
    )

    controller.start()
    try:
        assert work_started.wait(timeout=0.5)
        assert heartbeat_seen.wait(timeout=0.5)
        time.sleep(1.1)
        snapshot = controller.binding_snapshots()[0]
        assert runner.heartbeat_calls >= 2
        assert snapshot["device"] == "hermes-device-a"
        assert snapshot["status"] == "在线 · Hermes 执行中"
    finally:
        release_work.set()
        controller.stop()


def test_startup_manager_creates_and_removes_startup_command(tmp_path):
    manager = StartupManager(
        startup_dir=tmp_path,
        shortcut_name="CodexBridgeTest.cmd",
    )
    exe_path = tmp_path / "CodexBridge.exe"

    manager.set_enabled(True, exe_path, args=["--start"])

    assert manager.is_enabled()
    command = (tmp_path / "CodexBridgeTest.cmd").read_text(encoding="utf-8")
    assert str(exe_path) in command
    assert "--start" in command

    manager.set_enabled(False, exe_path)

    assert not manager.is_enabled()


def test_startup_manager_defaults_to_hidden_vbs_startup(tmp_path):
    manager = StartupManager(startup_dir=tmp_path)
    exe_path = tmp_path / "中文 安装" / "CodexBridge.exe"

    manager.set_enabled(True, exe_path, args=["--start"])

    command = (tmp_path / "CodexBridge.vbs").read_text(encoding="utf-16")
    assert "WScript.Shell" in command
    assert ", 0, False" in command
    assert str(exe_path) in command
    assert "--start" in command

    manager.set_enabled(False, exe_path)

    assert not manager.is_enabled()


def test_startup_cleanup_keeps_other_install_and_removes_legacy(tmp_path):
    manager = StartupManager(startup_dir=tmp_path)
    exe = tmp_path / "current" / "CodexBridge.exe"
    manager.enable(exe, args=["--start"])
    manager.remove_if_owned(tmp_path / "other" / "CodexBridge.exe")
    assert manager.is_enabled()
    manager.legacy_command_path.write_text(f'@echo off\nstart "" "{exe}" --start\n', encoding="utf-8")
    manager.remove_if_owned(exe)
    assert not manager.is_enabled()
