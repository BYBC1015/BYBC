from pathlib import Path

from codex_bridge.config import BridgeConfig
from codex_bridge.launcher import CodexLauncher
from codex_bridge.network_guard import (
    NetworkGuard,
    ProbeResult,
    find_default_cmynetwork_path,
)
from codex_bridge.tool_locator import (
    find_default_codex_path,
    first_existing_path,
    parse_codex_exe_path,
)


class FakeHealthProbe:
    def __init__(self, results):
        self.results = list(results)
        self.urls = []

    def check(self, url, timeout_seconds):
        self.urls.append((url, timeout_seconds))
        return self.results.pop(0)


class FakeProcessProbe:
    def __init__(self, running=True):
        self.running = running
        self.names = []

    def is_running(self, process_name):
        self.names.append(process_name)
        return self.running


class FakeActionRunner:
    def __init__(self):
        self.started = []
        self.commands = []

    def start_exe(self, path):
        self.started.append(Path(path))

    def run_command(self, command):
        self.commands.append(command)


def make_config(tmp_path, **overrides):
    values = {
        "hub": "http://hub.example/hub",
        "token": "secret",
        "prompt_dir": tmp_path / "prompts",
        "state_path": tmp_path / "state.json",
        "projects": {},
        "ignore_existing_on_first_run": False,
        "vpn_failure_threshold": 2,
        "vpn_recovery_cooldown_seconds": 300,
    }
    values.update(overrides)
    return BridgeConfig(**values)


def test_network_guard_uses_chatgpt_health_url_and_cmynetwork_name(tmp_path):
    config = make_config(tmp_path)
    health = FakeHealthProbe([ProbeResult(ok=True, detail="HTTP 200")])
    process = FakeProcessProbe(running=True)
    actions = FakeActionRunner()
    guard = NetworkGuard(
        config,
        health_probe=health,
        process_probe=process,
        action_runner=actions,
        now=lambda: 1000.0,
    )

    result = guard.check()

    assert result.ok is True
    assert health.urls == [("https://chatgpt.com/", config.vpn_check_timeout_seconds)]
    assert process.names == ["cmynetwork"]
    assert actions.started == []
    assert actions.commands == []


def test_network_guard_notifies_after_failures_without_touching_vpn_by_default(tmp_path):
    config = make_config(tmp_path, vpn_auto_recover=False)
    health = FakeHealthProbe(
        [
            ProbeResult(ok=False, detail="timeout"),
            ProbeResult(ok=False, detail="timeout"),
        ]
    )
    actions = FakeActionRunner()
    guard = NetworkGuard(
        config,
        health_probe=health,
        process_probe=FakeProcessProbe(running=True),
        action_runner=actions,
        now=lambda: 1000.0,
    )

    first = guard.check()
    second = guard.check()

    assert first.action == "none"
    assert second.action == "notify"
    assert "cmynetwork" in second.message
    assert "自动恢复已关闭" in second.message
    assert actions.started == []
    assert actions.commands == []


def test_network_guard_can_start_cmynetwork_when_auto_recover_is_enabled(tmp_path):
    vpn_exe = tmp_path / "cmynetwork.exe"
    vpn_exe.write_text("fake", encoding="utf-8")
    config = make_config(tmp_path, vpn_exe_path=vpn_exe, vpn_auto_recover=True)
    health = FakeHealthProbe(
        [
            ProbeResult(ok=False, detail="timeout"),
            ProbeResult(ok=False, detail="timeout"),
        ]
    )
    actions = FakeActionRunner()
    guard = NetworkGuard(
        config,
        health_probe=health,
        process_probe=FakeProcessProbe(running=False),
        action_runner=actions,
        now=lambda: 1000.0,
    )

    guard.check()
    result = guard.check()

    assert result.action == "start_vpn"
    assert actions.started == [vpn_exe]


def test_network_guard_prefers_custom_switch_command_when_configured(tmp_path):
    config = make_config(
        tmp_path,
        vpn_switch_command='powershell -NoProfile -Command "Write-Output switch"',
        vpn_auto_recover=True,
    )
    health = FakeHealthProbe(
        [
            ProbeResult(ok=False, detail="timeout"),
            ProbeResult(ok=False, detail="timeout"),
        ]
    )
    actions = FakeActionRunner()
    guard = NetworkGuard(
        config,
        health_probe=health,
        process_probe=FakeProcessProbe(running=True),
        action_runner=actions,
        now=lambda: 1000.0,
    )

    guard.check()
    result = guard.check()

    assert result.action == "switch_command"
    assert actions.commands == ['powershell -NoProfile -Command "Write-Output switch"']


def test_find_default_cmynetwork_path_returns_standard_windows_path_when_present(
    monkeypatch,
):
    candidates = []

    def fake_exists(path):
        candidates.append(Path(path))
        return str(path).endswith(r"CMYNetwork\cmynetwork.exe")

    monkeypatch.setattr(Path, "exists", fake_exists)

    path = find_default_cmynetwork_path()

    assert path is not None
    assert str(path).endswith(r"CMYNetwork\cmynetwork.exe")


def test_first_existing_path_returns_first_existing_candidate(tmp_path):
    missing = tmp_path / "missing.exe"
    existing = tmp_path / "Codex.exe"
    existing.write_text("fake", encoding="utf-8")

    assert first_existing_path([missing, existing]) == existing


def test_find_default_codex_path_can_scan_supplied_candidates(tmp_path):
    codex = tmp_path / "OpenAI.Codex_1.0" / "app" / "Codex.exe"
    codex.parent.mkdir(parents=True)
    codex.write_text("fake", encoding="utf-8")

    assert find_default_codex_path(candidates=[codex]) == codex


def test_parse_codex_exe_path_prefers_desktop_app_over_resource_binary():
    output = "\n".join(
        [
            r"C:\Program Files\WindowsApps\OpenAI.Codex_1.0\app\resources\codex.exe",
            r"C:\Program Files\WindowsApps\OpenAI.Codex_1.0\app\ChatGPT.exe",
        ]
    )

    assert parse_codex_exe_path(output) == Path(
        r"C:\Program Files\WindowsApps\OpenAI.Codex_1.0\app\ChatGPT.exe"
    )


def test_codex_launcher_uses_configured_exe_as_windows_fallback(tmp_path, monkeypatch):
    codex = tmp_path / "Codex.exe"
    codex.write_text("fake", encoding="utf-8")
    opened = []
    launched = []

    def failing_startfile(url):
        opened.append(url)
        raise OSError("no protocol")

    class FakePopen:
        def __init__(self, args, **kwargs):
            launched.append((args, kwargs))

    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("os.startfile", failing_startfile, raising=False)
    monkeypatch.setattr("subprocess.Popen", FakePopen)

    CodexLauncher(codex).open("codex://new?path=x&prompt=y")

    assert opened == ["codex://new?path=x&prompt=y"]
    assert launched[0][0] == [str(codex), "codex://new?path=x&prompt=y"]
