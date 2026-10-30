import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import pytest

from codex_bridge import local_tools


@pytest.fixture(scope="module")
def tk_root():
    # Keep one Tcl interpreter for this test process; production windows each
    # run in their own process. Reinitializing Tcl after patched environments
    # can leave its Windows environment cache stale.
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


def test_frozen_location_is_install_directory_not_unpack_or_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "CodexBridge.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", "C:/temporary/unpacked", raising=False)
    assert local_tools.install_root() == tmp_path


@pytest.mark.parametrize("tool", local_tools.TOOLS)
def test_launch_with_spaces_chinese_and_clean_runtime(tmp_path, monkeypatch, tool):
    root = tmp_path / "中文 安装目录"
    exe = local_tools.tool_path(tool.key, root)
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"test fixture")
    calls = []
    resolve = tmp_path / "达芬奇 运行环境"
    monkeypatch.setattr(local_tools, "find_resolve_root", lambda: resolve)
    monkeypatch.setenv("ELECTRON_RUN_AS_NODE", "1")
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: calls.append((args, kw)))
    local_tools.launch_tool(tool.key, root)
    args, options = calls[0]
    if tool.key == "color-workshop":
        assert args == [str(resolve / "Electron/electron.exe"), str(exe.parent)]
        assert "ELECTRON_RUN_AS_NODE" not in options["env"]
        assert options["env"]["HUB_RESOLVE_ROOT"] == str(resolve)
        assert not (exe.parent / "runtime.json").exists()
    elif tool.key == "beat-workshop":
        assert args == [str(resolve / "Electron/electron.exe"), str(exe.parent)]
        assert "ELECTRON_RUN_AS_NODE" not in options["env"]
        import json
        assert json.loads((exe.parent / "runtime.json").read_text(encoding="utf-8"))["resolveRoot"] == str(resolve)
    else:
        assert args == [str(exe)]
    assert options["cwd"] == str(exe.parent)
    assert options["shell"] is False
    assert options["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_missing_tool_reports_reinstall_without_falling_back_to_old_locations(tmp_path):
    with pytest.raises(FileNotFoundError, match="重新安装"):
        local_tools.launch_tool("kling-batch", tmp_path)
    with pytest.raises(ValueError):
        local_tools.tool_path("../../elsewhere", tmp_path)


def test_tools_mode_does_not_acquire_bridge_mutex(monkeypatch, tmp_path):
    from codex_bridge import cli
    called = []
    monkeypatch.setattr(local_tools, "main", lambda config: called.append(config) or 0)
    monkeypatch.setattr(cli.SingleInstance, "acquire", lambda self: pytest.fail("mutex acquired"))
    assert cli.main(["--tools", "--config", str(tmp_path / "config.json")]) == 0
    assert called == [tmp_path / "config.json"]


def test_all_buttons_launch_and_fit_minimum_window(monkeypatch, tk_root):
    root = tk.Toplevel(tk_root)
    try:
        root.geometry("720x490")
        parent = ttk.Frame(root, padding=20)
        parent.pack(fill="both", expand=True)
        calls = []
        monkeypatch.setattr(local_tools, "launch_tool", lambda key: calls.append(key))
        buttons = local_tools.populate_tools(parent)
        root.update()
        for key, button in buttons.items():
            assert button.winfo_ismapped()
            assert button.winfo_rooty() + button.winfo_height() <= root.winfo_rooty() + root.winfo_height()
            button.invoke()
        assert calls == [tool.key for tool in local_tools.TOOLS]
    finally:
        root.destroy()


def test_toolbox_launch_keeps_bridge_running_and_resets_runtime(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Bridge 安装" / "CodexBridge.exe"))
    Path(sys.executable).parent.mkdir()
    Path(sys.executable).write_bytes(b"installed companion")
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: calls.append((args, kw)))
    config = tmp_path / "用户 配置.json"
    local_tools.launch_toolbox(config)
    args, options = calls[0]
    assert args == [sys.executable, "--tools", "--config", str(config)]
    assert options["shell"] is False
    assert options["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_desktop_toolbox_launch_uses_bridge_entry_point_not_desktop(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    desktop = tmp_path / "带空格 安装目录" / "HubDesktop.exe"
    desktop.parent.mkdir()
    bridge = desktop.with_name("CodexBridge.exe")
    bridge.write_bytes(b"installed companion")
    monkeypatch.setattr(sys, "executable", str(desktop))
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: calls.append((args, kw)))
    config = tmp_path / "用户配置" / "config.json"
    local_tools.launch_toolbox(config)
    args, options = calls[0]
    assert args == [str(bridge), "--tools", "--config", str(config)]
    assert options["shell"] is False
    assert "startupinfo" not in options  # Must not SW_HIDE the requested GUI.
    if sys.platform == "win32":
        assert options["creationflags"] == subprocess.CREATE_NO_WINDOW


def test_toolbox_missing_companion_never_relaunches_desktop(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "HubDesktop.exe"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("wrong entry point launched"))
    with pytest.raises(FileNotFoundError, match="重新安装 Codex Hub"):
        local_tools.launch_toolbox(tmp_path / "config.json")


def test_source_toolbox_launch_uses_module_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: calls.append((args, kw)))
    config = tmp_path / "config.json"
    local_tools.launch_toolbox(config)
    assert calls[0][0] == [sys.executable, "-m", "codex_bridge.cli", "--tools", "--config", str(config)]


def test_bridge_main_page_keeps_original_tabs_and_opens_toolbox(tmp_path, monkeypatch, tk_root):
    from unittest.mock import MagicMock
    from codex_bridge import gui
    monkeypatch.setattr(gui.tk, "Tk", lambda: tk.Toplevel(tk_root))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    controller = MagicMock(is_running=False, status_text="已停止")
    controller.binding_snapshots.return_value = []
    monkeypatch.setattr(gui, "BridgeController", lambda *a, **kw: controller)
    tray = MagicMock(error=None)
    tray.start.return_value = False
    monkeypatch.setattr(gui, "BridgeTray", lambda **kw: tray)
    calls = []
    monkeypatch.setattr(local_tools, "launch_toolbox", lambda config: calls.append(config))
    app = gui.BridgeApp(tmp_path / "config.json")
    try:
        app.root.geometry("900x650")
        app.root.update()
        notebook = app.main_notebook
        assert [notebook.tab(tab, "text") for tab in notebook.tabs()] == ["AI 绑定", "网络与系统", "运行日志"]
        assert notebook.select() == str(app.ai_tab)
        button = app.toolbox_button
        assert button.winfo_ismapped()
        assert button.winfo_rootx() >= app.root.winfo_rootx()
        assert button.winfo_rootx() + button.winfo_width() <= app.root.winfo_rootx() + app.root.winfo_width()
        button.invoke()
        assert calls == [app.config_path]
        controller.start.assert_not_called()
        controller.stop.assert_not_called()
    finally:
        app.exit_app()


def test_music_missing_runtime_reports_dependency(tmp_path, monkeypatch):
    target = local_tools.tool_path("beat-workshop", tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text("")
    def missing():
        raise FileNotFoundError("请先安装达芬奇")
    monkeypatch.setattr(local_tools, "find_resolve_root", missing)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("must not launch"))
    with pytest.raises(FileNotFoundError, match="达芬奇"):
        local_tools.launch_tool("beat-workshop", tmp_path)
