"""Toolbox lifecycle tests use fake Tk; no foreground window or worker runs."""
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from codex_bridge import local_tools
from codex_bridge.desktop_window import DesktopInstance


def test_toolbox_identity_is_config_scoped_and_separate_from_desktop(tmp_path):
    first = local_tools._toolbox_instance(tmp_path / 'config.json')
    alias = local_tools._toolbox_instance(tmp_path / '.' / 'config.json')
    other = local_tools._toolbox_instance(tmp_path / 'other.json')
    assert first.event_name == alias.event_name
    assert first.event_name != other.event_name
    assert '.toolbox-' in first.event_name


def test_repeated_toolbox_clicks_deduplicate_startup_then_wake_existing(tmp_path, monkeypatch):
    instance = Mock(); instance.request_wake.return_value = False
    monkeypatch.setattr(local_tools, '_toolbox_instance', lambda _: instance)
    monkeypatch.setattr(local_tools, '_toolbox_processes', {})
    process = Mock(); process.poll.return_value = None
    spawn = Mock(return_value=process)
    monkeypatch.setattr(local_tools, '_spawn_toolbox', spawn)
    config = tmp_path / 'config.json'
    # Includes clicks arriving before the child has created its native event.
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(local_tools.launch_toolbox, [config] * 8)) == [process] * 8
    spawn.assert_called_once_with(config.resolve())
    instance.request_wake.return_value = True
    assert local_tools.launch_toolbox(config) is process
    assert instance.request_wake.call_count == 9
    spawn.assert_called_once()
    # A genuinely closed toolbox can be opened again without the Bridge worker.
    process.poll.return_value = 0
    instance.request_wake.return_value = False
    local_tools.launch_toolbox(config)
    assert spawn.call_count == 2


def test_other_entry_point_wakes_toolbox_without_spawning(tmp_path, monkeypatch):
    instance = Mock(); instance.request_wake.return_value = True
    monkeypatch.setattr(local_tools, '_toolbox_instance', lambda _: instance)
    monkeypatch.setattr(local_tools, '_toolbox_processes', {})
    spawn = Mock(); monkeypatch.setattr(local_tools, '_spawn_toolbox', spawn)
    assert local_tools.launch_toolbox(tmp_path / 'config.json') is None
    spawn.assert_not_called()


def test_secondary_toolbox_signals_owner_without_creating_tk(tmp_path, monkeypatch):
    instance = Mock(); instance.acquire.return_value = False
    monkeypatch.setattr(local_tools, '_toolbox_instance', lambda _: instance)
    create = Mock(side_effect=AssertionError('second Tk window'))
    monkeypatch.setattr(local_tools.tk, 'Tk', create)
    assert local_tools.main(tmp_path / 'config.json') == 0
    instance.close.assert_called_once()
    create.assert_not_called()


def test_minimized_toolbox_wakes_on_tk_thread_and_releases_owner(tmp_path, monkeypatch):
    instance = Mock(); instance.acquire.return_value = True
    instance.watch.side_effect = lambda callback: callback()
    monkeypatch.setattr(local_tools, '_toolbox_instance', lambda _: instance)
    root = Mock(); callbacks = []
    root.after.side_effect = lambda _delay, callback: callbacks.append(callback)
    ui_thread = threading.get_ident()
    seen_threads = []
    for method in (root.deiconify, root.lift, root.focus_force):
        method.side_effect = lambda: seen_threads.append(threading.get_ident())
    def loop():
        instance._watcher.join(timeout=2)
        callbacks.pop(0)()
    root.mainloop.side_effect = loop
    monkeypatch.setattr(local_tools.tk, 'Tk', lambda: root)
    monkeypatch.setattr(local_tools.ttk, 'Frame', Mock())
    monkeypatch.setattr(local_tools, 'populate_tools', Mock())
    monkeypatch.setattr('codex_bridge.gui.set_window_icon', Mock())
    assert local_tools.main(tmp_path / 'config.json') == 0
    assert seen_threads == [ui_thread] * 3
    assert instance._watcher.daemon
    instance.close.assert_called_once()
    root.destroy.assert_called_once()


def test_toolbox_start_failure_releases_its_own_instance(tmp_path, monkeypatch):
    instance = Mock(); instance.acquire.return_value = True
    monkeypatch.setattr(local_tools, '_toolbox_instance', lambda _: instance)
    monkeypatch.setattr(local_tools.tk, 'Tk', Mock(side_effect=RuntimeError('isolated init failure')))
    with pytest.raises(RuntimeError, match='isolated init failure'):
        local_tools.main(tmp_path / 'config.json')
    instance.close.assert_called_once()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows named events')
def test_native_toolbox_wake_and_singleton_without_any_window():
    # Unique test-owned mutex/events never signal an installed app or user job.
    key = 'toolbox-test-' + uuid.uuid4().hex
    owner = DesktopInstance(key)
    contender = DesktopInstance(key)
    received = threading.Event()
    try:
        assert owner.acquire()
        owner._watcher = threading.Thread(target=owner.watch, args=(received.set,), daemon=True)
        owner._watcher.start()
        assert contender.request_wake()
        assert received.wait(2)
        received.clear()
        assert not contender.acquire()
        assert received.wait(2)
    finally:
        contender.close()
        owner.close()
    assert not owner._watcher.is_alive()
    replacement = DesktopInstance(key)
    try:
        assert replacement.acquire()
    finally:
        replacement.close()
