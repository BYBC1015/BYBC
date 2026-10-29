import base64
import json
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codex_bridge import __version__
from codex_bridge import desktop_window as desktop


@pytest.mark.parametrize('url', [
    'https://hub.example/hub', 'https://hub.example/hub/projects?p=1#thread',
    'https://HUB.example:443/hub/files/1/preview',
])
def test_navigation_keeps_hub_and_results_in_window(url):
    assert desktop.HubLocation('https://hub.example/hub/').trusted(url)


@pytest.mark.parametrize('url', [
    'https://hub.example.evil/hub', 'https://hub.example/hub-extra',
    'https://hub.example/other', 'http://hub.example/hub',
    'https://hub.example:444/hub', 'https://user:password@hub.example/hub',
    'https://hub.example/hub/%2e%2e/private', 'https://hub.example/hub/%5cprivate',
    'file:///C:/Windows', 'javascript:alert(1)', 'data:text/html,test', None,
])
def test_navigation_rejects_other_origins_paths_and_local_schemes(url):
    assert not desktop.HubLocation('https://hub.example/hub').trusted(url)


@pytest.mark.parametrize(('url', 'browser'), [
    ('https://chatgpt.com/', 'chrome'), ('https://github.com/', 'chrome'),
    ('https://news.qq.com/', 'edge'), ('https://www.example.cn/', 'edge'),
    ('https://www.deepseek.com/', 'edge'), ('http://127.0.0.1:9000/', 'edge'),
    ('http://localhost:9000/', 'edge'), ('https://qq.com.evil.com/', 'chrome'),
])
def test_browser_routing_respects_user_network_split(url, browser):
    assert desktop.browser_for(url) == browser


def test_external_browser_failure_never_falls_back_to_system_browser(monkeypatch):
    monkeypatch.setattr(desktop, 'browser_executable', Mock(side_effect=FileNotFoundError()))
    popen = Mock()
    monkeypatch.setattr(desktop.subprocess, 'Popen', popen)
    with pytest.raises(FileNotFoundError):
        desktop.open_external('https://github.com/')
    popen.assert_not_called()
    with pytest.raises(ValueError):
        desktop.open_external('file:///C:/Windows')


@pytest.fixture
def host(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    result = desktop.DesktopHost(desktop.HubLocation('https://hub.example/hub'),
        {'project-id': project}, tmp_path / 'desktop', external_opener=Mock())
    result.window = Mock()
    result.window.get_current_url.return_value = 'https://hub.example/hub/projects'
    result._first_load = False
    return result


def test_native_project_api_only_opens_configured_directories(host, monkeypatch):
    startfile = Mock()
    monkeypatch.setattr(desktop.os, 'startfile', startfile, raising=False)
    api = desktop.DesktopAPI(host)
    assert api.get_version() == __version__
    assert api.list_projects() == [{'id': 'project-id', 'name': 'project', 'available': True}]
    assert api.open_project('project-id') == {'opened': True}
    startfile.assert_called_once_with(str(host.projects['project-id']))
    with pytest.raises(ValueError):
        api.open_project('C:/Windows')
    with pytest.raises(ValueError):
        api.open_project({'command': 'cmd'})
    assert not hasattr(api, 'execute')


@pytest.mark.parametrize('current', ['https://other.example/', 'https://hub.example/other', None, 'about:blank'])
def test_native_api_checks_current_top_level_page_on_every_call(host, current):
    api = desktop.DesktopAPI(host)
    host.window.get_current_url.return_value = current
    for call in [api.info, api.get_version, api.list_projects, lambda: api.open_project('project-id')]:
        with pytest.raises(PermissionError):
            call()


def test_offline_page_can_only_retry_its_hub(host):
    api = desktop.DesktopAPI(host)
    host.window.get_current_url.return_value = 'about:blank'
    host._local_page = True
    navigated = threading.Event()
    host.window.load_url.side_effect = lambda url: navigated.set()
    assert api.retry_connection() == {'connecting': True}
    assert navigated.wait(2)
    host.window.load_url.assert_called_once_with(host.location.home)
    with pytest.raises(PermissionError):
        api.retry_connection()


def test_internal_preview_is_allowed_and_external_is_cancelled(host):
    internal = SimpleNamespace(Uri='https://hub.example/hub/files/1/preview', NavigationId=10, Cancel=False)
    host.on_navigation(None, internal)
    assert not internal.Cancel
    done = threading.Event()
    host.external_opener = lambda url: done.set()
    external = SimpleNamespace(Uri='https://github.com/', NavigationId=11, Cancel=False)
    host.on_navigation(None, external)
    assert external.Cancel
    assert done.wait(2)
    host.on_navigation_completed(None, SimpleNamespace(NavigationId=11, IsSuccess=False))
    host.window.load_html.assert_not_called()


def test_cancelled_older_navigation_does_not_replace_newer_page(host):
    host.on_navigation(None, SimpleNamespace(Uri=host.location.home, NavigationId=10, Cancel=False))
    host.on_navigation(None, SimpleNamespace(Uri=host.location.home, NavigationId=11, Cancel=False))
    host.on_navigation_completed(None, SimpleNamespace(NavigationId=10, IsSuccess=False))
    host.window.load_html.assert_not_called()


def test_failed_navigation_shows_retry_screen(host):
    done = threading.Event()
    host.window.load_html.side_effect = lambda content: done.set()
    host.on_navigation(None, SimpleNamespace(Uri=host.location.home, NavigationId=5, Cancel=False))
    host.on_navigation_completed(None, SimpleNamespace(NavigationId=5, IsSuccess=False))
    assert done.wait(2)
    page = host.window.load_html.call_args.args[0]
    assert '暂时连接不上 Hub' in page and 'retry_connection' in page
    assert host._local_page


def test_webview2_builtin_html_data_navigation_is_allowed_but_arbitrary_data_is_not(host):
    host.show_offline('Reconnect')
    target = 'data:text/html;charset=utf-8;base64,' + base64.b64encode(host._local_html.encode()).decode()
    args = SimpleNamespace(Uri=target, NavigationId=50, Cancel=False)
    host.on_navigation(None, args)
    assert not args.Cancel
    injected = SimpleNamespace(Uri='data:text/html;charset=utf-8;base64,PGgxPkV2aWw8L2gxPg==', NavigationId=51, Cancel=False)
    host.on_navigation(None, injected)
    assert injected.Cancel
    host._local_page = False
    assert not host.is_local_html_navigation(target)


def test_late_remote_loaded_event_does_not_cancel_pending_offline_document(host):
    host.show_offline('Reconnect')
    host.on_loaded()
    assert host._local_page
    host.window.run_js.assert_not_called()


def test_reconnecting_to_same_home_explicitly_reloads_webview(host):
    host.navigate_home()
    host.window.run_js.assert_called_once_with('window.location.reload()')
    host.window.load_url.assert_not_called()


def test_internal_popup_preserves_main_chat_and_external_popup_routes_browser(host):
    opened = threading.Event()
    host.open_preview = Mock(side_effect=lambda url: opened.set())
    args = SimpleNamespace(Uri=host.location.base + '/files/1', Handled=False)
    host.on_new_window(None, args)
    assert opened.wait(2) and args.Handled
    host.window.load_url.assert_not_called()
    host.open_preview.assert_called_once_with(args.Uri)
    host.launch_external = Mock()
    host.on_new_window(None, SimpleNamespace(Uri='https://github.com/', Handled=False))
    host.launch_external.assert_called_once_with('https://github.com/')


def test_download_is_not_treated_as_offline_navigation(host):
    host._active_navigation = 20
    host.restore_workspace = Mock()
    host.on_download_starting(None, SimpleNamespace(DownloadOperation=SimpleNamespace(Uri=host._navigation_target)))
    assert 20 in host._cancelled_navigations
    host.on_navigation_completed(None, SimpleNamespace(NavigationId=20, IsSuccess=False))
    host.window.load_html.assert_not_called()
    host.restore_workspace.assert_not_called()


def test_offline_text_is_escaped():
    page = desktop.offline_page('<script>bad()</script>', connecting=False)
    assert '<script>bad()</script>' not in page


def test_geometry_survives_restart_but_recovers_from_disconnected_screen(tmp_path):
    path = tmp_path / 'window.json'
    desktop._atomic_json(path, {'width': 1500, 'height': 950, 'x': 100, 'y': 80, 'maximized': True})
    screen = SimpleNamespace(x=0, y=0, width=1920, height=1080)
    assert desktop.read_geometry(path, [screen]) == {'width': 1500, 'height': 950, 'x': 100, 'y': 80, 'maximized': True}
    desktop._atomic_json(path, {'width': 90000, 'height': -2, 'x': 9000, 'y': 8000})
    geometry = desktop.read_geometry(path, [screen])
    assert geometry == {'width': 1920, 'height': 620, 'x': None, 'y': None, 'maximized': False}
    path.write_text('bad data')
    assert desktop.read_geometry(path)['width'] == 1380


def test_launch_is_nonblocking_and_never_includes_credentials(tmp_path, monkeypatch):
    popen = Mock()
    monkeypatch.setattr(desktop.subprocess, 'Popen', popen)
    result = desktop.launch_desktop(tmp_path / 'config.json', 'https://hub.example/hub')
    assert result is popen.return_value
    command = popen.call_args.args[0]
    assert command[:3] == [sys.executable, '-m', 'codex_bridge.desktop_window']
    assert '--config' in command and '--hub' in command
    assert not any('token' in part for part in command)
    assert 'startupinfo' not in popen.call_args.kwargs


def test_frozen_launch_requires_the_bundled_component(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(desktop.sys, 'executable', str(tmp_path / 'CodexBridge.exe'))
    with pytest.raises(FileNotFoundError):
        desktop.launch_desktop(tmp_path / 'config.json')
    executable = tmp_path / 'HubDesktop.exe'
    executable.touch()
    popen = Mock()
    monkeypatch.setattr(desktop.subprocess, 'Popen', popen)
    desktop.launch_desktop(tmp_path / 'config.json')
    assert popen.call_args.args[0][0] == str(executable)
    assert popen.call_args.kwargs['env']['PYINSTALLER_RESET_ENVIRONMENT'] == '1'
    assert 'startupinfo' not in popen.call_args.kwargs


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows native lifecycle')
def test_second_launch_wakes_existing_window_and_safe_exit_can_close_it():
    key = 'test-' + uuid.uuid4().hex
    first = desktop.DesktopInstance(key)
    second = desktop.DesktopInstance(key)
    wake, close = threading.Event(), threading.Event()
    try:
        assert first.acquire()
        first._watcher = threading.Thread(target=first.watch, args=(wake.set, close.set), daemon=True)
        first._watcher.start()
        assert not second.acquire()
        assert wake.wait(2)
        assert second.request_close()
        assert close.wait(2)
    finally:
        second.close()
        first.close()
    replacement = desktop.DesktopInstance(key)
    try:
        assert replacement.acquire()
    finally:
        replacement.close()


def test_safe_exit_only_addresses_the_configured_hub(tmp_path, monkeypatch):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'hub': 'https://hub.example/hub', 'token': 'device-secret'}))
    instance = Mock()
    monkeypatch.setattr(desktop, 'DesktopInstance', instance)
    assert desktop.request_desktop_close(config) is instance.return_value.request_close.return_value
    instance.assert_called_once_with(desktop.HubLocation('https://hub.example/hub').key)
