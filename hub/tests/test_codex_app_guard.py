from types import SimpleNamespace

from codex_bridge.codex_app_probe import WindowsCodexAppProbe, _powershell_literal


def test_process_probe_accepts_only_explicit_running_marker(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="CODEX_DESKTOP_RUNNING\n")

    monkeypatch.setattr("codex_bridge.codex_app_probe.sys.platform", "win32")
    monkeypatch.setattr("codex_bridge.codex_app_probe.subprocess.run", fake_run)

    assert WindowsCodexAppProbe().is_running() is True
    assert calls[0][0][:3] == ["powershell.exe", "-NoProfile", "-NonInteractive"]
    command = calls[0][0][-1]
    assert "--type=" in command
    assert "OpenAI\\.Codex_" in command


def test_process_probe_returns_false_without_marker(monkeypatch):
    monkeypatch.setattr("codex_bridge.codex_app_probe.sys.platform", "win32")
    monkeypatch.setattr(
        "codex_bridge.codex_app_probe.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=""),
    )

    assert WindowsCodexAppProbe().is_running() is False


def test_powershell_literal_escapes_configured_path():
    assert _powershell_literal("C:/O'Brien/Codex.exe") == "C:/O''Brien/Codex.exe"
