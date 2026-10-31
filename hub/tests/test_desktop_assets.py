import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from codex_bridge.desktop_assets import PackagedAssets, WorkspaceCache, build_asset_manifest, offline_document
from codex_bridge.desktop_window import DesktopHost, HubLocation, DesktopAPI


def page(user='1', csrf='server-secret'):
    return '<!doctype html><html><head></head><body><main data-ws-shell data-user="' + user + '">A conversation</main><input name="_csrf" value="' + csrf + '"></body></html>'


def test_snapshot_preserves_conversation_but_never_csrf_and_is_explicitly_offline(tmp_path):
    cache = WorkspaceCache(tmp_path)
    assert cache.put('https://hub/hub/projects/one', page())
    restored = cache.get('https://hub/hub/projects/one')
    assert 'A conversation' in restored['html']
    assert 'data-hub-offline="true"' in restored['html']
    assert 'server-secret' not in restored['html']
    assert 'server-secret' not in next(tmp_path.glob('*.html')).read_text()
    assert not cache.put('https://hub/hub/login', '<form>login</form>')


def test_accounts_do_not_share_snapshots_logout_invalidates_inflight_capture(tmp_path):
    cache = WorkspaceCache(tmp_path)
    url = 'https://hub/hub/projects/one'
    cache.put(url, page('1'))
    cache.put(url + '?other', page('2'))
    assert cache.get(url) is None
    generation = cache.generation
    cache.clear_current()
    assert cache.get('', latest=True) is None
    assert not cache.put(url, page('2'), generation=generation)
    assert cache.latest_verified is None


def test_snapshot_eviction_and_oversize_limit(tmp_path):
    cache = WorkspaceCache(tmp_path)
    cache.MAX_PAGES = 2
    for number in range(3):
        cache.put('https://hub/hub/projects/' + str(number), page())
    assert cache.get('https://hub/hub/projects/0') is None
    assert len(list(tmp_path.glob('*.html'))) == 2
    cache.MAX_PAGE_BYTES = 10
    assert not cache.put('https://hub/hub/projects/large', page())


def test_bundle_matches_path_query_and_content_and_resolves_css_dependency(tmp_path):
    static = tmp_path / 'static'
    templates = tmp_path / 'templates'
    static.mkdir(); templates.mkdir()
    (static / 'app.js').write_text('window.nativeAsset=true')
    (static / 'app.css').write_text('body{background:url("bg.png")}')
    (static / 'bg.png').write_bytes(b'image')
    (templates / 'page.html').write_text("{{ app_path('/static/app.js') }}?v=2\" {{ app_path('/static/app.css') }}?v=1\"")
    manifest = build_asset_manifest(static, templates)
    assert manifest['assets']['bg.png']['queries'] == ['']
    (static / 'desktop-assets.json').write_text(json.dumps(manifest))
    assets = PackagedAssets(static)
    hub = HubLocation('https://hub/hub')
    identity = manifest['assets']['app.js']['sha256'][:16]
    assert assets.read(hub, hub.base + '/static/app.js?v=' + identity)[0] == b'window.nativeAsset=true'
    assert assets.read(hub, hub.base + '/static/app.js?v=2') is None
    assert assets.read(hub, hub.base + '/static/app.js?v=3') is None
    assert assets.read(hub, 'https://other/hub/static/app.js?v=2') is None
    assert assets.read(hub, hub.base + '/static/%2e%2e/private') is None
    (static / 'app.js').write_text('changed after manifest')
    assert assets.read(hub, hub.base + '/static/app.js?v=2') is None


def test_offline_api_requires_real_recent_same_account_workspace(tmp_path):
    host = DesktopHost(HubLocation('https://hub/hub'), {}, tmp_path)
    host.window = Mock()
    host.window.get_current_url.return_value = 'https://hub/hub/projects/one'
    host.window.evaluate_js.side_effect = lambda script: '1' if 'data-ws-shell' in script else True
    host._offline = True
    host._offline_account = host.cache.account_key('1')
    api = DesktopAPI(host)
    assert api.workspace_online('1') == {'online': False}
    host.cache.put('https://hub/hub/projects/one', page('2'))
    assert api.workspace_online('1') == {'online': False}
    host.cache.put('https://hub/hub/projects/one', page('1'))
    assert api.workspace_online('2') == {'online': False}
    assert api.workspace_online('1') == {'online': True}
    assert host._offline is False


def test_offline_writes_are_503_but_probe_get_never_uses_snapshot(tmp_path):
    host = DesktopHost(HubLocation('https://hub/hub'), {}, tmp_path)
    host._offline = True
    host._resource_response = Mock(return_value='blocked')
    post = SimpleNamespace(Request=SimpleNamespace(Uri='https://hub/hub/projects/one/dispatch', Method='POST'), Response=None)
    host.on_resource_requested(None, post)
    assert post.Response == 'blocked'
    assert host._resource_response.call_args.args[2] == 503
    host._offline_response = {'url': 'https://hub/hub/projects/one', 'html': page()}
    probe = SimpleNamespace(Request=SimpleNamespace(Uri='https://hub/hub/projects/one', Method='GET'), ResourceContext='Fetch', Response=None)
    host.on_resource_requested(None, probe)
    assert probe.Response is None
    assert host._offline_response is not None


class Headers:
    def __init__(self, **values):
        self.values = values
    def Contains(self, name):
        return name in self.values
    def GetHeader(self, name):
        return self.values.get(name, '')
    def SetHeader(self, name, value):
        self.values[name] = value


def test_late_pre_logout_network_response_cannot_reactivate_old_account(tmp_path):
    host = DesktopHost(HubLocation('https://hub/hub'), {}, tmp_path)
    host.cache.put('https://hub/hub/projects/one', page())
    request = SimpleNamespace(Uri='https://hub/hub/projects/one', Method='GET', Headers=Headers())
    host.on_resource_requested(None, SimpleNamespace(Request=request))
    assert request.Headers.GetHeader('X-Hub-Desktop-Cache-Epoch') == '0'
    host.cache.clear_current()
    response = SimpleNamespace(StatusCode=200, Headers=Headers(**{'Content-Type': 'text/html'}), GetContentAsync=Mock())
    host.on_resource_response(None, SimpleNamespace(Request=request, Response=response))
    response.GetContentAsync.assert_not_called()
    assert host.cache.get('', latest=True) is None


def test_offline_session_can_log_in_again_after_real_login_page_load(tmp_path):
    host = DesktopHost(HubLocation('https://hub/hub'), {}, tmp_path)
    host._offline = True
    get = SimpleNamespace(Uri='https://hub/hub/login', Method='GET', Headers=Headers())
    host.on_resource_requested(None, SimpleNamespace(Request=get))
    response = SimpleNamespace(StatusCode=200, Headers=Headers(**{'Content-Type': 'text/html'}))
    host.on_resource_response(None, SimpleNamespace(Request=get, Response=response))
    post = SimpleNamespace(Request=SimpleNamespace(Uri=get.Uri, Method='POST', Headers=Headers()), Response=None)
    host.on_resource_requested(None, post)
    assert post.Response is None
    assert host._offline is True  # Authentication permission never unlocks workspace writes.


def test_recent_response_before_disconnect_cannot_unlock_cached_document(tmp_path):
    host = DesktopHost(HubLocation('https://hub/hub'), {}, tmp_path)
    host.window = Mock()
    host.cache.put('https://hub/hub/projects/one', page())
    host._offline = True
    host._offline_since = time.time() + 1
    assert not host.confirm_workspace_online('1')
    host.window.evaluate_js.assert_not_called()


def test_delayed_restore_never_replaces_a_newer_navigation(tmp_path):
    host = DesktopHost(HubLocation('https://hub/hub'), {}, tmp_path)
    host.window = Mock()
    host._active_navigation = 2
    host.restore_workspace(1, 'https://hub/hub/projects/one')
    host.window.load_url.assert_not_called()
    host.window.load_html.assert_not_called()
    host.window.run_js.assert_not_called()


def test_navigation_changed_while_reading_snapshot_prevents_restore(tmp_path):
    host = DesktopHost(HubLocation('https://hub/hub'), {}, tmp_path)
    host.window = Mock()
    host._active_navigation = 1
    def get(url):
        host._active_navigation = 2
        return {'url': url, 'html': page(), 'account': '1'}
    host.cache.get = get
    host.restore_workspace(1, 'https://hub/hub/projects/one')
    host.window.load_url.assert_not_called()
    assert host._offline is False


@pytest.mark.parametrize('change', ['logout', 'switch-account', 'logout-and-same-account'])
def test_account_change_during_snapshot_preparation_cancels_restore(tmp_path, change):
    host = DesktopHost(HubLocation('https://hub/hub'), {}, tmp_path)
    host.window = Mock()
    url = 'https://hub/hub/projects/one'
    assert host.cache.put(url, page('1'))
    def prepare(location, snapshot):
        if change == 'switch-account':
            host.cache.put(url, page('2'))
        else:
            host.cache.clear_current()
            if change == 'logout-and-same-account': host.cache.put(url, page('1'))
        return snapshot
    host.assets.prepare_snapshot = prepare
    host.restore_workspace(target=url)
    assert not host._offline and host._offline_response is None
    host.window.load_url.assert_not_called()
    host.window.run_js.assert_not_called()


@pytest.mark.parametrize('change', ['none', 'logout', 'switch-account', 'logout-and-same-account'])
def test_pending_offline_document_is_fenced_against_account_changes(tmp_path, change):
    host = DesktopHost(HubLocation('https://hub/hub'), {}, tmp_path)
    host.window = Mock()
    url = 'https://hub/hub/projects/one'
    host.cache.put(url, page('1'))
    host.assets.prepare_snapshot = lambda _location, snapshot: snapshot
    host.restore_workspace(target=url)
    assert host._offline_response is not None
    host._resource_response = Mock(return_value='isolated-html-response')
    if change == 'switch-account':
        host.cache.put(url, page('2'))
    elif change != 'none':
        host.cache.clear_current()
        if change == 'logout-and-same-account': host.cache.put(url, page('1'))
    request = SimpleNamespace(Request=SimpleNamespace(Uri=url, Method='GET'),
                              ResourceContext='Document', Response=None)
    host.on_resource_requested(None, request)
    assert host._offline_response is None
    if change == 'none':
        assert request.Response == 'isolated-html-response'
    else:
        assert request.Response is None
        host._resource_response.assert_not_called()


def test_corrupt_or_partial_snapshot_is_cache_miss(tmp_path):
    cache = WorkspaceCache(tmp_path)
    url = 'https://hub/hub/projects/one'
    cache.put(url, page())
    path = next(tmp_path.glob('*.html'))
    path.write_bytes(b'\xff\xfe')
    assert cache.get(url) is None
    path.write_text(page()[:100], encoding='utf-8')
    assert cache.get(url) is None
    (tmp_path / 'index.json').write_bytes(b'\xff')
    assert cache.get(url) is None


def test_large_authenticated_response_unlocks_online_without_snapshot(tmp_path):
    host = DesktopHost(HubLocation('https://hub/hub'), {}, tmp_path)
    host.window = Mock()
    host.window.evaluate_js.side_effect = lambda script: '1' if 'data-ws-shell' in script else False
    host.cache.MAX_PAGE_BYTES = 10
    assert host.cache.put('https://hub/hub/projects/one', page()) is False
    assert host.cache.get('', latest=True) is None
    assert host.confirm_workspace_online('1') is True


def test_disk_failure_does_not_disable_authenticated_online_send(tmp_path, monkeypatch):
    host = DesktopHost(HubLocation('https://hub/hub'), {}, tmp_path)
    host.window = Mock()
    host.window.evaluate_js.side_effect = lambda script: '1' if 'data-ws-shell' in script else False
    monkeypatch.setattr(host.cache, '_persist_snapshot', Mock(side_effect=OSError('disk full')))
    assert host.cache.put('https://hub/hub/projects/one', page()) is False
    assert host.cache.get('', latest=True) is None
    assert host.confirm_workspace_online('1') is True


def test_failed_snapshot_on_new_account_never_restores_previous_account(tmp_path, monkeypatch):
    cache = WorkspaceCache(tmp_path)
    url = 'https://hub/hub/projects/one'
    assert cache.put(url, page('1'))
    epoch = cache.generation
    monkeypatch.setattr(cache, '_persist_snapshot', Mock(side_effect=OSError('disk full')))
    assert not cache.put(url, page('2'), generation=epoch)
    assert cache.latest_verified['user'] == '2'
    assert cache.get(url) is None
    assert not cache.put(url, page('1'), generation=epoch)
    assert cache.latest_verified['user'] == '2'
