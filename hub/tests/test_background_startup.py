"""Tray-only startup contracts without creating a real foreground window."""
import sys
import threading
from types import SimpleNamespace

import pytest

from codex_bridge import cli, gui
from codex_bridge.tray import BridgeTray
from test_agent_attachments import png
from test_codex_bridge import allow_test_cli_instance


def app_fixture(tmp_path, monkeypatch, *, tray_ready=True, configured=True, background=True):
    events = []
    class Root:
        def __init__(self):
            self.callbacks = []
            events.append('root-created')
        def withdraw(self): events.append('withdraw')
        def title(self, *_): pass
        def geometry(self, *_): pass
        def minsize(self, *_): pass
        def protocol(self, *_): pass
        def destroy(self): events.append('destroy')
        def after(self, delay, callback): self.callbacks.append(callback)
        def mainloop(self):
            events.append('mainloop')
            for callback in self.callbacks: callback()
    class Tray:
        is_running = tray_ready
        error = RuntimeError('isolated tray failure') if not tray_ready else None
        def __init__(self, **kwargs): self.callbacks = kwargs
        def start(self, **kwargs):
            events.append(('tray-start', kwargs))
            return tray_ready
        def stop(self): events.append('tray-stop')
    root = Root()
    config = SimpleNamespace(runtime_configs=lambda: [object()] if configured else [])
    monkeypatch.setattr(gui.tk, 'Tk', lambda: root)
    monkeypatch.setattr(gui, 'load_config', lambda _: config)
    monkeypatch.setattr(gui, 'set_window_icon', lambda _: None)
    monkeypatch.setattr(gui, 'StartupManager', lambda: SimpleNamespace(
        migrate_legacy=lambda *_a, **_k: events.append('migrate-startup')))
    monkeypatch.setattr(gui, 'PersistentLog', lambda *_: SimpleNamespace(tail=lambda _: []))
    monkeypatch.setattr(gui, 'BridgeController', lambda *_a, **_k: SimpleNamespace())
    monkeypatch.setattr(gui, 'AgentService', lambda **_: SimpleNamespace(
        configured=configured, start=lambda: events.append('agent-start')))
    monkeypatch.setattr(gui, 'BridgeTray', Tray)
    for name in ('_init_variables', '_configure_style', '_build_ui', 'refresh_ui'):
        monkeypatch.setattr(gui.BridgeApp, name, lambda *_: None)
    monkeypatch.setattr(gui.BridgeApp, 'append_log', lambda _, message: events.append(('log', message)))
    monkeypatch.setattr(gui.BridgeApp, 'start', lambda _: events.append('worker-start'))
    monkeypatch.setattr(gui.BridgeApp, '_open_desktop_workspace', lambda _: events.append('desktop-open'))
    monkeypatch.setattr(gui.messagebox, 'showwarning', lambda *_: events.append('warning-dialog'))
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'CodexBridge.exe'))
    (tmp_path / 'HubDesktop.exe').write_bytes(b'isolated desktop marker')
    config_path = tmp_path / 'config.json'
    config_path.write_text('{}')
    return gui.BridgeApp(config_path, auto_start=True, background=background), events


def test_background_is_hidden_before_tray_and_never_opens_workspace(tmp_path, monkeypatch):
    app, events = app_fixture(tmp_path, monkeypatch)
    assert app.run() == 0
    assert events.index('withdraw') < events.index(('tray-start', {'wait_until_ready': 2}))
    assert events.index(('tray-start', {'wait_until_ready': 2})) < events.index('worker-start')
    assert 'desktop-open' not in events and 'warning-dialog' not in events
    assert 'migrate-startup' not in events
    assert callable(app.tray.callbacks['show_window']) and callable(app.tray.callbacks['exit_app'])


@pytest.mark.parametrize('failure', ['tray', 'configuration'])
def test_background_failure_exits_without_orphan_worker_or_dialog(tmp_path, monkeypatch, failure):
    app, events = app_fixture(tmp_path, monkeypatch,
        tray_ready=failure != 'tray', configured=failure != 'configuration')
    assert app.run() == 2
    assert 'worker-start' not in events and 'agent-start' not in events
    assert 'mainloop' not in events and 'desktop-open' not in events and 'warning-dialog' not in events
    assert 'destroy' in events and 'tray-stop' in events
    assert any(isinstance(row, tuple) and row[0] == 'log' and '后台启动未完成' in row[1] for row in events)


def test_normal_start_still_opens_the_workspace(tmp_path, monkeypatch):
    app, events = app_fixture(tmp_path, monkeypatch, background=False)
    assert app.run() == 0
    assert 'withdraw' not in events
    assert 'worker-start' in events and 'desktop-open' in events


def test_cli_routes_background_without_changing_normal_start(monkeypatch):
    allow_test_cli_instance(monkeypatch)
    calls = []
    monkeypatch.setattr(cli, 'gui_main', lambda *a, **kw: calls.append(kw) or 0)
    assert cli.main(['--start', '--background']) == 0
    assert calls == [{'auto_start': True, 'background': True}]


def test_existing_background_instance_never_opens_or_notifies(monkeypatch):
    monkeypatch.setattr(cli, 'SingleInstance', lambda: SimpleNamespace(acquire=lambda: False))
    def unexpected(*_a, **_k): raise AssertionError('Background startup attempted visible UI')
    monkeypatch.setattr(cli, 'notify_already_running', unexpected)
    monkeypatch.setattr(cli, 'gui_main', unexpected)
    monkeypatch.setattr('codex_bridge.desktop_window.launch_desktop', unexpected)
    assert cli.main(['--background']) == 0
    assert cli.main(['--start', '--background']) == 0


@pytest.mark.parametrize('mode', ['--init', '--once', '--cli', '--ui', '--tools'])
def test_background_rejects_conflicting_cli_modes(mode):
    with pytest.raises(SystemExit) as result:
        cli.main(['--background', mode])
    assert result.value.code == 2


@pytest.mark.parametrize('mode', ['ready', 'error', 'late'])
def test_background_tray_requires_actual_ready_and_rejects_late_setup(tmp_path, monkeypatch, mode):
    icons = []
    class Icon:
        def __init__(self, *_):
            self._visible = False
            self.stops = 0
            self.setup = None
            icons.append(self)
        @property
        def visible(self): return self._visible
        @visible.setter
        def visible(self, value):
            if mode == 'error': raise OSError('tray registration failed')
            self._visible = value
        def run_detached(self, setup=None):
            assert threading.current_thread().daemon
            self.setup = setup
            if mode != 'late': setup(self)
        def stop(self): self.stops += 1
    monkeypatch.setitem(sys.modules, 'pystray', SimpleNamespace(Icon=Icon,
        Menu=lambda *args: args, MenuItem=lambda *a, **kw: a))
    image = tmp_path / 'owl.png'
    image.write_bytes(png())
    tray = BridgeTray(icon_path=image, title='Isolated tray', **{key: lambda: None for key in
        ('show_window', 'start_bridge', 'stop_bridge', 'open_hub', 'exit_app')})
    assert tray.start(wait_until_ready=.01) == (mode == 'ready')
    if mode == 'ready':
        assert icons[0].visible and tray.is_running
        tray.stop()
    else:
        assert not tray.is_running and tray.icon is None and icons[0].stops == 1
        assert tray.error is not None
        if mode == 'late':
            icons[0].setup(icons[0])
            assert not icons[0].visible
