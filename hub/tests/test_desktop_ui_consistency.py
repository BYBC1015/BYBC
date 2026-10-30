"""Offline UI compatibility without opening or changing the real desktop profile."""
import hashlib
import json
import re
from pathlib import Path
from unittest.mock import Mock
from types import SimpleNamespace

import pytest

from codex_bridge.desktop_assets import PackagedAssets, SnapshotLayout, WorkspaceCache, build_asset_manifest
from codex_bridge.desktop_window import DesktopAPI, DesktopHost, HubLocation
from codex_hub.static_assets import asset_digest
from test_mobile_composer_browser import live_hub  # noqa: F401
from test_workspace_shell_browser import login_page
from workspace_fixture import seed_workspace


def bundled(tmp_path):
    static, templates = tmp_path / 'static', tmp_path / 'templates'
    static.mkdir(); templates.mkdir()
    (static / 'shell.css').write_text('body{color:#eee;background:#101112}', encoding='utf-8')
    (static / 'shell.js').write_text('window.__currentShell=true;', encoding='utf-8')
    (templates / 'project_workspace.html').write_text(
        '<link href="{{ asset_path(\'/static/shell.css\') }}"><script src="{{ asset_path(\'/static/shell.js\') }}"></script>', encoding='utf-8')
    manifest = build_asset_manifest(static, templates)
    (static / 'desktop-assets.json').write_text(json.dumps(manifest), encoding='utf-8')
    return static, templates, manifest


def standard_old_page():
    markers = ' '.join(SnapshotLayout.REQUIRED - {'data-ws-shell'})
    return ('<!doctype html><html><head><title>旧对话</title><link href="/hub/static/shell.css?v=20261029-local1">'
        '<script src="/hub/static/shell.js?v=20261029-recovery2"></script></head><body>'
        '<main data-ws-shell data-user="7" data-project="project" data-conversation="6" '
        'data-workspace-url="/hub/projects/project/conversations/6"><div ' + markers + '></div>'
        '<input name="request_key" value="stable-send-key"><input name="title" value=""><textarea name="description">草稿</textarea>'
        '<input name="_csrf" value="private-csrf"><article class="ws-message ws-message-user"><div>原始消息 &lt;hello&gt;</div></article>'
        '</main></body></html>')


def test_frozen_bundle_has_current_workspace_manifest_without_templates(tmp_path, monkeypatch):
    static, templates, manifest = bundled(tmp_path)
    assert manifest['assets']['shell.js']['queries'] == ['v=' + asset_digest(static, 'shell.js')]
    (templates / 'project_workspace.html').unlink()
    monkeypatch.setattr('sys.frozen', True, raising=False)
    assets = PackagedAssets(static)
    cache = WorkspaceCache(tmp_path / 'cache')
    url = 'https://hub.test/hub/projects/project/conversations/6'
    assert cache.put(url, standard_old_page())
    before = next(cache.directory.glob('*.html')).read_bytes()
    result = assets.prepare_snapshot(HubLocation('https://hub.test/hub'), cache.get(url))
    assert result['upgraded'] and not result['read_only']
    assert '20261029-' not in result['html']
    assert '原始消息 &lt;hello&gt;' in result['html']
    assert 'stable-send-key' in result['html'] and '>草稿</textarea>' in result['html']
    assert 'private-csrf' not in result['html']
    assert 'data-user="7" data-project="project" data-conversation="6"' in result['html']
    assert next(cache.directory.glob('*.html')).read_bytes() == before
    for resource in re.findall(r'(?:src|href)="(https://[^\"]+/static/[^\"]+)"', result['html']):
        assert assets.read(HubLocation('https://hub.test/hub'), resource), resource


def test_mutable_legacy_query_cannot_override_new_server_asset(tmp_path):
    static, _, manifest = bundled(tmp_path)
    manifest['assets']['shell.js']['queries'].append('v=old-manual')
    (static / 'desktop-assets.json').write_text(json.dumps(manifest), encoding='utf-8')
    assets = PackagedAssets(static)
    location = HubLocation('https://hub.test/hub')
    assert assets.read(location, location.base + '/static/shell.js?v=old-manual') is None
    newer_hash = hashlib.sha256(b'new server script').hexdigest()[:16]
    assert assets.read(location, location.base + '/static/shell.js?v=' + newer_hash) is None
    assert assets.read(location, location.base + '/static/shell.js?v=' + asset_digest(static, 'shell.js'))


def test_multiline_windows_snapshot_and_exact_legacy_crlf_recovery(tmp_path):
    cache = WorkspaceCache(tmp_path)
    url = 'https://hub.test/hub/projects/project'
    source = standard_old_page().replace('><', '>\n<')
    assert cache.put(url, source)
    target = next(tmp_path.glob('*.html'))
    stored = target.read_bytes()
    index = json.loads((tmp_path / 'index.json').read_text(encoding='utf-8'))['pages'][target.name]
    assert len(stored) == index['bytes']
    assert hashlib.sha256(stored).hexdigest() == index['sha256']
    assert cache.get(url)
    # Reproduce write_text's old Windows representation, without modifying its
    # persisted length/digest. Only this exact legacy round-trip is recoverable.
    target.write_bytes(stored.replace(b'\n', b'\r\n'))
    assert cache.get(url)
    target.write_bytes(target.read_bytes().replace(b'stable-send-key', b'injected-key'))
    assert cache.get(url) is None


@pytest.mark.parametrize('future', [False, True])
def test_unknown_or_newer_shell_uses_current_readonly_records_without_old_scripts(tmp_path, future):
    static, _, _ = bundled(tmp_path)
    source = standard_old_page()
    if future:
        source = source.replace('data-ws-shell ', 'data-ws-shell data-ws-release="999.0.0" ')
    else:
        source = source.replace('data-ws-input', 'old-input')
    source = source.replace('</main>', '<script>window.unsafeOldScript=true</script></main>')
    cache = WorkspaceCache(tmp_path / 'cache')
    url = 'https://hub.test/hub/projects/project/conversations/6'
    cache.put(url, source)
    result = PackagedAssets(static).prepare_snapshot(HubLocation('https://hub.test/hub'), cache.get(url))
    assert result['read_only'] and not result['upgraded']
    assert 'data-hub-cache-readonly="true"' in result['html']
    assert '原始消息 &lt;hello&gt;' in result['html']
    assert 'unsafeOldScript' not in result['html'] and '20261029-' not in result['html']
    assert '<form' not in result['html'] and 'private-csrf' not in result['html']
    assert 'data-user="7"' in result['html']
    assert cache.get(url)  # No migration deletes the original account-scoped record.
    cache.put(url, source.replace('data-user="7"', 'data-user="8"'))
    assert cache.get(url)['account'] == cache.account_key('8')


def test_toolbox_api_keeps_config_and_denies_foreign_page_and_closed_host(tmp_path, monkeypatch):
    process = Mock(); process.poll.return_value = None
    launch = Mock(return_value=process)
    monkeypatch.setattr('codex_bridge.local_tools.launch_toolbox', launch)
    config = tmp_path / 'local-config.json'
    host = DesktopHost(HubLocation('https://hub.test/hub'), {}, tmp_path, config_path=config)
    host.window = Mock(); host.window.get_current_url.return_value = 'https://hub.test/hub/projects/project'
    api = DesktopAPI(host)
    assert api.open_toolbox() == {'opened': True}
    assert api.open_toolbox() == {'opened': True}
    launch.assert_called_once_with(config.resolve())
    host.window.get_current_url.return_value = 'https://other.test/'
    with pytest.raises(PermissionError): api.open_toolbox()
    host._closed.set()
    with pytest.raises(PermissionError): host.open_toolbox()


@pytest.mark.parametrize('interruption', ['new-navigation', 'close'])
def test_delayed_snapshot_preparation_cannot_mark_new_page_offline(tmp_path, interruption):
    host = DesktopHost(HubLocation('https://hub.test/hub'), {}, tmp_path)
    host.window = Mock()
    target = 'https://hub.test/hub/projects/old'
    host.on_navigation(None, SimpleNamespace(Uri=target, NavigationId=1, Cancel=False))
    host.cache.get = Mock(return_value={'url':target, 'account':'old-user', 'html':standard_old_page()})
    def prepare(location, snapshot):
        # This is the actual interleaving: an event arrives after disk reading,
        # while parsing/hashing the old snapshot on the restoration worker.
        if interruption == 'close': host.close_children()
        else: host.on_navigation(None, SimpleNamespace(Uri='https://hub.test/hub/projects/new', NavigationId=2, Cancel=False))
        return snapshot
    host.assets.prepare_snapshot = prepare
    host.restore_workspace(1, target)
    assert host._offline is False
    assert host._offline_account is None
    assert host._offline_response is None
    host.window.get_current_url.assert_not_called()
    host.window.load_url.assert_not_called()
    host.window.run_js.assert_not_called()


def test_actual_workspace_can_recover_old_layout_offline_with_draft_and_file(live_hub, repo, tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    data = seed_workspace(repo, room_messages=3)
    location = HubLocation(live_hub)
    assets = PackagedAssets()
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1380, 'height': 900})
            errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
            login_page(page, live_hub)
            url = f'{live_hub}/projects/workspace-team/conversations/{data["conversation_id"]}'
            response = page.goto(url)
            source = response.text()
            # Same structural contract and obsolete resource versions as the
            # user's 10/29 conversation cache; no private user content is copied.
            source = re.sub(r'\?v=[a-f0-9]{16}', '?v=20261029-local1', source)
            source = re.sub(r' data-ws-release="[^"]*"', '', source)
            source = source.replace('data-ws-toolbox-entry', 'old-toolbox-entry')
            count = page.locator('[data-ws-entry]').count()
            page.locator('[data-ws-input]').fill('跨网络恢复必须保留草稿')
            page.locator('[data-ws-attachment-input]').set_input_files({'name': 'brief.txt', 'mimeType': 'text/plain', 'buffer': b'local draft attachment'})
            page.wait_for_function("""async () => { const s=document.querySelector('[data-ws-shell]').dataset;
                const saved=await CodexWorkspaceStore.read(`${s.user}.${s.project}.${s.conversation}`);
                return saved.draft?.description==='跨网络恢复必须保留草稿' && saved.draft.files?.length===1; }""")
            cache = WorkspaceCache(tmp_path / 'snapshots'); assert cache.put(url, source)
            restored = assets.prepare_snapshot(location, cache.get(url))
            assert restored['upgraded']
            def offline(route):
                if route.request.resource_type == 'document' and route.request.url == url:
                    route.fulfill(status=200, content_type='text/html', body=restored['html'])
                else:
                    resource = assets.read(location, route.request.url)
                    if resource: route.fulfill(status=200, content_type=resource[1], body=resource[0])
                    else: route.fulfill(status=503, content_type='application/json', body='{}')
            page.route('**/*', offline)
            page.reload(wait_until='domcontentloaded')
            page.wait_for_function("document.querySelector('[data-ws-input]').value==='跨网络恢复必须保留草稿'")
            assert page.locator('[data-ws-entry]').count() == count
            assert 'brief.txt' in page.locator('[data-ws-attachments]').inner_text()
            assert page.locator('html').get_attribute('data-hub-offline') == 'true'
            assert page.locator('[data-ws-sidebar]').evaluate("el => getComputedStyle(el).borderRadius") == '24px'
            assert not errors, errors
        finally:
            browser.close()
