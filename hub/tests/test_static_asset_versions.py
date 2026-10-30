import hashlib
import re

from codex_hub.main import BASE_DIR
from codex_hub.static_assets import asset_digest, asset_url


def test_content_version_changes_when_same_named_file_changes(tmp_path):
    asset = tmp_path / 'screen.css'
    asset.write_text('body {color:red}', encoding='utf-8')
    first = asset_url(tmp_path, '/static/screen.css')
    asset.write_text('body {color:blue}', encoding='utf-8')
    second = asset_url(tmp_path, '/static/screen.css')
    assert first != second
    assert second.endswith(hashlib.sha256(asset.read_bytes()).hexdigest()[:16])


def test_only_matching_content_versions_are_immutable(client):
    path = '/static/workspace-shell.css'
    url = asset_url(BASE_DIR / 'static', path)
    current = client.get(url)
    assert current.status_code == 200
    assert 'immutable' in current.headers['cache-control']
    for query in ('', '?v=20261029-local1', '?v=0000000000000000'):
        response = client.get(path + query)
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-cache, must-revalidate'
    missing = client.get('/static/missing.css?v=0000000000000000')
    assert missing.status_code == 404
    assert 'immutable' not in missing.headers['cache-control']


def test_login_uses_same_origin_content_versioned_resources(client):
    response = client.get('/login', headers={'X-Forwarded-Prefix': '/hub'})
    paths = re.findall(r'(?:src|href)="([^"]*/static/[^"?]+)\?v=([0-9a-f]{16})"', response.text)
    assert paths
    for path, version in paths:
        relative = path.split('/static/', 1)[1]
        assert version == asset_digest(BASE_DIR / 'static', relative)
    assert not re.search(r'(?:src|href)="https?://[^" ]+\.(?:js|css)', response.text)


def test_cached_legacy_redirect_is_repaired_without_replaying_post(client):
    headers = {'X-Forwarded-Prefix':'/hub'}
    for path in ('/hub/projects/demo', '/hub/hub/projects/demo'):
        response = client.get(path + '?next=%2Fhub%2F&x=1', headers=headers, follow_redirects=False)
        assert response.status_code == 307
        assert response.headers['location'] == '/hub/projects/demo?next=%2Fhub%2F&x=1'
        assert response.headers['cache-control'] == 'no-store'
    assert client.post('/hub/projects/demo', headers=headers, follow_redirects=False).status_code == 404
    assert client.get('/hub/projects/demo', follow_redirects=False).status_code == 404
    static = client.head('/hub/static/workspace-shell.css?v=x', headers=headers, follow_redirects=False)
    assert static.status_code == 307
    assert static.headers['location'] == '/hub/static/workspace-shell.css?v=x'
    assert static.headers['cache-control'] == 'no-store'
