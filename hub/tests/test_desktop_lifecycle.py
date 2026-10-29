"""Offline native-host regressions: no Tk, WebView, browser or live profile."""
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codex_bridge import desktop_window as desktop
from codex_bridge.desktop_assets import PackagedAssets, WorkspaceCache
from test_desktop_window import host
from test_reading_browser import page


class DeferredTimer:
    instances = []

    def __init__(self, delay, callback):
        self.callback = callback
        self.cancelled = False
        self.instances.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def timers(monkeypatch):
    DeferredTimer.instances = []
    monkeypatch.setattr(desktop.threading, 'Timer', DeferredTimer)
    return DeferredTimer.instances


def navigate(host, url, number=1):
    host.on_navigation(None, SimpleNamespace(Uri=url, NavigationId=number, Cancel=False))


def test_retry_preserves_conversation_and_does_not_return_home(host, timers):
    room = host.location.base + '/projects/demo/conversations/7?view=chat#message-42'
    navigate(host, room)
    host.show_offline('暂时断开')
    host.window.get_current_url.return_value = 'about:blank'
    assert desktop.DesktopAPI(host).retry_connection() == {'connecting': True}
    timers[-1].callback()
    host.window.load_url.assert_called_once_with(room)


@pytest.mark.parametrize('transient', ['/login?next=/projects', '/downloads/windows',
    '/projects/demo/attachments/3', '/projects/demo/attachments/3?download=1',
    '/api/projects/demo/tasks/3', '/projects/demo/export.md'])
def test_retry_never_replays_login_download_or_api_request(host, timers, transient):
    room = host.location.base + '/projects/demo/conversations/7'
    navigate(host, room)
    navigate(host, host.location.base + transient, 2)
    host.show_offline('暂时断开')
    host.window.get_current_url.return_value = 'about:blank'
    desktop.DesktopAPI(host).retry_connection()
    timers[-1].callback()
    host.window.load_url.assert_called_once_with(room)


def test_multiple_retry_clicks_are_coalesced_and_close_cancels_pending_retry(host, timers):
    api = desktop.DesktopAPI(host)
    api.retry_connection()
    api.retry_connection()
    host.close_children()
    # Simulate already queued timer callbacks even after Timer.cancel().
    for timer in timers:
        timer.callback()
    host.window.load_url.assert_not_called()
    host.window.run_js.assert_not_called()
    host.window.get_current_url.reset_mock()
    with pytest.raises(PermissionError):
        api.info()
    host.window.get_current_url.assert_not_called()


def test_multiple_retry_clicks_only_navigate_once_and_same_room_reloads(host, timers):
    room = host.location.base + '/projects/demo/conversations/7'
    navigate(host, room)
    host.window.get_current_url.return_value = room
    for _ in range(3):
        desktop.DesktopAPI(host).retry_connection()
    for timer in timers:
        timer.callback()
    host.window.run_js.assert_called_once_with('window.location.reload()')
    host.window.load_url.assert_not_called()


def test_inline_preview_reconnect_keeps_its_own_document(host, timers):
    preview = host.location.base + '/projects/demo/attachments/3?inline=true'
    navigate(host, preview)
    host.show_offline('暂时断开')
    host.window.get_current_url.return_value = 'about:blank'
    desktop.DesktopAPI(host).retry_connection()
    timers[-1].callback()
    host.window.load_url.assert_called_once_with(preview)


def test_new_user_navigation_invalidates_queued_retry_without_reloading_draft(host, timers):
    desktop.DesktopAPI(host).retry_connection()
    room = host.location.base + '/projects/demo/conversations/8'
    navigate(host, room)
    timers[-1].callback()
    host.window.load_url.assert_not_called()
    host.window.run_js.assert_not_called()


def test_close_clears_children_once_and_late_popup_cannot_launch(host):
    child = Mock()
    host._children.append(child)
    host.close_children()
    host.close_children()
    host.open_preview = Mock()
    args = SimpleNamespace(Uri=host.location.base + '/projects/demo/attachments/3?inline=true', Handled=False)
    host.on_new_window(None, args)
    assert args.Handled
    child.close_children.assert_called_once()
    child.window.destroy.assert_called_once()
    host.open_preview.assert_not_called()
    assert host._children == []


def test_window_restore_uses_the_saved_monitor_dimensions(tmp_path):
    path = tmp_path / 'window.json'
    desktop._atomic_json(path, {'width': 1800, 'height': 1100, 'x': 1400, 'y': 70})
    screens = [SimpleNamespace(x=0, y=0, width=1280, height=720),
               SimpleNamespace(x=1280, y=0, width=2560, height=1440)]
    restored = desktop.read_geometry(path, screens)
    assert restored == {'width': 1800, 'height': 1100, 'x': 1400, 'y': 70, 'maximized': False}


def test_close_during_snapshot_read_prevents_all_window_callbacks(host):
    room = host.location.base + '/projects/demo/conversations/7'
    navigate(host, room)
    def snapshot(_url):
        host.close_children()
        return {'url': room, 'account': '1', 'html': '<html>cached</html>'}
    host.cache.get = snapshot
    host.restore_workspace(1, room)
    host.on_loaded()
    host.on_navigation_completed(None, SimpleNamespace(NavigationId=1, IsSuccess=False))
    host.bring_forward()
    host.window.load_html.assert_not_called()
    host.window.load_url.assert_not_called()
    host.window.run_js.assert_not_called()
    host.window.show.assert_not_called()


def test_preview_created_concurrently_with_parent_close_is_destroyed(host, monkeypatch):
    child_window = Mock()
    def create(*_args, **_kwargs):
        host.close_children()
        return child_window
    fake = SimpleNamespace(create_window=Mock(side_effect=create))
    monkeypatch.setitem(sys.modules, 'webview', fake)
    monkeypatch.setitem(sys.modules, 'webview.menu', SimpleNamespace(Menu=Mock(), MenuAction=Mock()))
    monkeypatch.setattr(desktop.DesktopHost, 'connect_native_events', Mock())
    host.open_preview(host.location.base + '/projects/demo/attachments/3?inline=true')
    child_window.destroy.assert_called_once()
    child_window.load_url.assert_not_called()
    assert host._children == []


def test_background_download_does_not_hide_an_unrelated_navigation_failure(host, monkeypatch):
    room = host.location.base + '/projects/demo/conversations/7'
    navigate(host, room)
    args = SimpleNamespace(DownloadOperation=SimpleNamespace(Uri=host.location.base + '/projects/demo/attachments/3'))
    host.on_download_starting(None, args)
    assert 1 not in host._cancelled_navigations
    host.restore_workspace = Mock()
    monkeypatch.setattr(desktop.threading, 'Thread', lambda target, args=(), **_: SimpleNamespace(start=lambda: target(*args)))
    host.on_navigation_completed(None, SimpleNamespace(NavigationId=1, IsSuccess=False))
    host.restore_workspace.assert_called_once_with(1, room)


def test_unknown_download_is_not_evidence_to_cancel_a_navigation(host):
    navigate(host, host.location.home)
    host.on_download_starting(None, SimpleNamespace())
    assert not host._cancelled_navigations


def test_close_between_account_queries_never_queries_destroyed_window(host):
    host.cache.verify_response(host.location.home,
        '<main data-ws-shell data-user="1"><input name="_csrf" value="test"></main>')
    def query(_script):
        host.close_children()
        return '1'
    host.window.evaluate_js.side_effect = query
    assert not host.confirm_workspace_online('1')
    assert host.window.evaluate_js.call_count == 1
    host.window.run_js.assert_not_called()


@pytest.mark.parametrize('broken', [[], {}, {'pages': {}}, {'account': None, 'pages': {'x': {}}},
    {'account': 'x', 'pages': {}}, {'account': None, 'pages': {('a'*24)+'-'+('b'*32)+'.html': {}}}])
def test_damaged_cache_index_is_a_cache_miss_and_preserves_other_profile_files(tmp_path, broken):
    tmp_path.joinpath('index.json').write_text(json.dumps(broken))
    draft = tmp_path / 'user-draft.txt'
    draft.write_text('unsent work')
    original_index = tmp_path.joinpath('index.json').read_bytes()
    cache = WorkspaceCache(tmp_path)
    assert cache.get('', latest=True) is None
    assert draft.read_text() == 'unsent work'
    assert tmp_path.joinpath('index.json').read_bytes() == original_index


@pytest.mark.parametrize('assets', [[], None, {'app.js': []}, {'app.js': {'queries': 'v=1', 'sha256': 'broken'}}])
def test_invalid_packaged_manifest_falls_through_to_network(tmp_path, assets):
    tmp_path.joinpath('desktop-assets.json').write_text(json.dumps({'assets': assets}))
    bundled = PackagedAssets(tmp_path)
    location = desktop.HubLocation('https://hub.example/hub')
    assert bundled.read(location, location.base + '/static/app.js?v=1') is None


def test_inaccessible_packaged_asset_falls_through_to_network(tmp_path, monkeypatch):
    import hashlib
    from pathlib import Path
    script = tmp_path / 'app.js'
    script.write_bytes(b'local-resource')
    manifest = {'app.js': {'queries': ['v=1'], 'sha256': hashlib.sha256(script.read_bytes()).hexdigest()}}
    tmp_path.joinpath('desktop-assets.json').write_text(json.dumps({'assets': manifest}))
    assets = PackagedAssets(tmp_path)
    original = Path.read_bytes
    def fail_read(path):
        if path == script:
            raise PermissionError('transient updater lock')
        return original(path)
    monkeypatch.setattr(Path, 'read_bytes', fail_read)
    location = desktop.HubLocation('https://hub.example/hub')
    assert assets.read(location, location.base + '/static/app.js?v=1') is None


def test_native_retry_page_handles_missing_bridge_rejection_and_reduced_motion(page):
    page.set_content(desktop.offline_page('尚未连接', connecting=False))
    page.get_by_role('button', name='重新连接').click()
    assert page.get_by_role('status').inner_text() == '桌面连接仍在准备，请稍后重试。'
    page.evaluate("window.pywebview={api:{retry_connection:()=>Promise.reject(new Error('closed'))}}")
    page.get_by_role('button', name='重新连接').click()
    page.wait_for_function("document.querySelector('#status').textContent==='连接尚未恢复，请稍后重试。'")
    assert page.get_by_role('button', name='重新连接').is_enabled()
    page.emulate_media(reduced_motion='reduce')
    page.set_content(desktop.offline_page())
    assert page.locator('.dot').evaluate("node=>getComputedStyle(node).animationName") == 'none'


def test_native_retry_page_coalesces_pending_clicks_and_does_not_claim_online(page):
    page.set_content(desktop.offline_page(connecting=False))
    page.evaluate("window.calls=0;window.pywebview={api:{retry_connection:()=>{window.calls++;return new Promise(resolve=>window.finishRetry=resolve);}}}")
    page.evaluate('retry();retry();retry()')
    assert page.evaluate('window.calls') == 1
    assert page.get_by_role('button', name='重新连接').is_disabled()
    assert page.get_by_role('status').inner_text() == '正在重新连接…'
    page.evaluate('window.finishRetry({connecting:true})')
    page.wait_for_function("!document.querySelector('#retry-button').disabled")
    assert page.get_by_role('status').inner_text() == '正在重新连接…'
    assert '已连接' not in page.locator('main').inner_text()
