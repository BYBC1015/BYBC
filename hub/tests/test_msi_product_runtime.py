"""Side-by-side entry and startup ownership; never acquire the live singleton."""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_bridge import cli, desktop_window
from codex_bridge.startup import StartupManager


def legacy(manager, exe, args=('--start',)):
    command = '@echo off\r\nstart "" ' + subprocess.list2cmdline([str(exe), *args]) + '\r\n'
    # Legacy generated commands always quote the executable, even without spaces.
    if not subprocess.list2cmdline([str(exe)]).startswith('"'):
        command = '@echo off\r\nstart "" "' + str(exe) + '"' + (' ' + subprocess.list2cmdline(list(args)) if args else '') + '\r\n'
    manager.legacy_command_path.write_text(command, encoding='utf-8')
    return manager.legacy_command_path.read_bytes()


def test_versioned_entry_with_existing_bridge_only_opens_current_companion(tmp_path, monkeypatch):
    root = tmp_path / 'Programs/CodexHub-MSI/1.3.0'
    root.mkdir(parents=True)
    (root / 'HubDesktop.exe').write_bytes(b'fixture')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(root / 'CodexBridge.exe'))
    monkeypatch.setattr(cli, 'SingleInstance', lambda: SimpleNamespace(acquire=lambda: False))
    monkeypatch.setattr(cli, 'gui_main', lambda *_a, **_k: pytest.fail('Duplicate Bridge GUI/worker'))
    monkeypatch.setattr(cli, 'notify_already_running', lambda: pytest.fail('Unexpected old-instance dialog'))
    calls = []
    monkeypatch.setattr(desktop_window.subprocess, 'Popen', lambda command, **kw: calls.append((command, kw)) or object())
    config = tmp_path / 'existing-config.json'; config.write_text('{}')
    before = config.read_bytes()
    assert cli.main(['--config', str(config)]) == 0
    assert calls[0][0] == [str(root / 'HubDesktop.exe'), '--config', str(config)]
    assert calls[0][1]['env']['PYINSTALLER_RESET_ENVIRONMENT'] == '1'
    assert config.read_bytes() == before


def test_versioned_entry_without_bridge_opens_gui_without_forcing_legacy_poller(tmp_path, monkeypatch):
    closed, launched = [], []
    monkeypatch.setattr(cli, 'SingleInstance', lambda: SimpleNamespace(acquire=lambda: True, close=lambda: closed.append(True)))
    monkeypatch.setattr(cli, 'gui_main', lambda config, **kw: launched.append((config, kw)) or 0)
    config = tmp_path / 'profile.json'
    assert cli.main(['--config', str(config)]) == 0
    assert launched == [(config, {'auto_start': False})] and closed == [True]


@pytest.mark.parametrize('case', ['same', 'case', 'quoted_args'])
def test_automatic_legacy_startup_migrates_only_same_target_and_arguments(tmp_path, case):
    manager = StartupManager(tmp_path)
    exe = tmp_path / '中文 目录/CodexBridge.exe'
    target = Path(str(exe).upper()) if case == 'case' else exe
    args = ['--start', '--config', str(tmp_path / '用户 配置.json')] if case == 'quoted_args' else ['--start']
    legacy(manager, target, args)
    manager.migrate_legacy(exe, args)
    assert not manager.legacy_command_path.exists()
    result = manager.command_path.read_text(encoding='utf-16')
    assert str(exe) in result and '--start' in result
    if case == 'quoted_args': assert str(tmp_path / '用户 配置.json') in result


@pytest.mark.parametrize('case', ['other_exe', 'different_args', 'extra_command', 'already_vbs', 'path_only_in_comment'])
def test_automatic_legacy_startup_preserves_other_installation_or_custom_script(tmp_path, case):
    manager = StartupManager(tmp_path)
    current = tmp_path / 'CodexHub-MSI/1.3.0/CodexBridge.exe'
    old = tmp_path / 'legacy 1.2.6/CodexBridge.exe' if case == 'other_exe' else current
    legacy(manager, old, ['--ui'] if case == 'different_args' else ['--start'])
    if case == 'extra_command':
        with manager.legacy_command_path.open('a', encoding='utf-8') as stream: stream.write('echo custom behavior\n')
    if case == 'path_only_in_comment': manager.legacy_command_path.write_text('REM "' + str(current) + '"\n', encoding='utf-8')
    if case == 'already_vbs': manager.command_path.write_bytes(b'existing independent startup')
    before = manager.legacy_command_path.read_bytes()
    manager.migrate_legacy(current, ['--start'])
    assert manager.legacy_command_path.read_bytes() == before
    if case == 'already_vbs': assert manager.command_path.read_bytes() == b'existing independent startup'
    else: assert not manager.command_path.exists()


def test_explicit_startup_toggle_can_select_new_installation(tmp_path):
    manager = StartupManager(tmp_path)
    current = tmp_path / 'new/CodexBridge.exe'
    legacy(manager, tmp_path / 'old/CodexBridge.exe')
    manager.set_enabled(True, current, ['--start'])
    assert str(current) in manager.command_path.read_text(encoding='utf-16')
    assert not manager.legacy_command_path.exists()
