"""Uploaded files stay inert while supported media remains previewable."""
import io
import shutil
import subprocess

import pytest
from PIL import Image

from codex_bridge.subprocess_utils import hidden_subprocess_kwargs
from test_workspace_shell import login
from test_mobile_composer_browser import live_hub  # noqa: F401


@pytest.fixture
def files_project(client, repo):
    device = repo.create_device(label='isolated-private-media-writer')
    token = repo.create_api_token(device_id=device['id'], name='private-media-test')
    project = repo.create_project(slug='private-media-test', name='Private media', created_by_device_id=device['id'])
    headers = {'Authorization': f'Bearer {token.raw}'}
    login(client)
    def upload(filename, content, mime):
        result = client.post('/api/projects/private-media-test/attachments', headers=headers,
                             files={'file': (filename, content, mime)})
        assert result.status_code == 201, result.text
        return result.json()['attachment']
    return project, headers, upload


def paths(attachment):
    leaf = f"/projects/private-media-test/attachments/{attachment['id']}"
    return leaf, '/api' + leaf


def assert_inert_headers(response):
    assert response.headers.get('x-content-type-options') == 'nosniff'
    csp = response.headers.get('content-security-policy', '')
    assert 'sandbox' in csp and 'allow-scripts' not in csp and 'allow-same-origin' not in csp


@pytest.mark.parametrize('filename,mime', [
    ('pretend.png', 'text/html'), ('pretend.mp4', 'image/svg+xml'),
    ('active.html', 'text/html'), ('active.svg', 'image/svg+xml'),
    ('pretend.html', 'image/png'), ('pretend.svg', 'video/mp4'),
    ('pretend.jpg', 'application/xhtml+xml'), ('pretend.webp', 'image/svg+xml; charset=utf-8'),
])
@pytest.mark.parametrize('size', ['', 'preview', 'thumb'])
def test_active_uploads_cannot_be_rendered_inline_on_web_or_api(client, files_project, filename, mime, size):
    _project, headers, upload = files_project
    body = b'<html><script>window.attachmentCodeExecuted=true</script></html>'
    attachment = upload(filename, body, mime)
    for path in paths(attachment):
        response = client.get(path, headers=headers, params={'inline': 'true', 'size': size})
        assert response.status_code == 200 and response.content == body
        assert response.headers['content-disposition'].startswith('attachment;'), dict(response.headers)
        assert_inert_headers(response)


@pytest.fixture(scope='module')
def real_video(tmp_path_factory):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.skip('ffmpeg is needed to create an actual decodable video fixture')
    path = tmp_path_factory.mktemp('media-fixture') / 'clip.mp4'
    subprocess.run([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                    '-i', 'color=c=navy:s=32x24:d=0.08', '-an', '-c:v', 'libx264',
                    '-pix_fmt', 'yuv420p', str(path)], check=True, timeout=20,
                   **hidden_subprocess_kwargs())
    return path.read_bytes()


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_real_media_inline_download_and_range_keep_original_bytes(client, files_project, real_video, kind):
    _project, headers, upload = files_project
    stream = io.BytesIO()
    Image.new('RGB', (32, 24), 'navy').save(stream, format='PNG')
    body, name, mime = ((stream.getvalue(), 'real.png', 'image/png') if kind == 'image'
                        else (real_video, 'real.mp4', 'video/mp4'))
    attachment = upload(name, body, mime)
    for path in paths(attachment):
        preview = client.get(path, headers=headers, params={'inline': 'true'})
        assert preview.status_code == 200 and preview.content == body
        assert preview.headers['content-type'] == mime
        assert preview.headers['content-disposition'].startswith('inline;')
        assert_inert_headers(preview)
        download = client.get(path, headers=headers)
        assert download.content == body and download.headers['content-disposition'].startswith('attachment;')
        assert_inert_headers(download)
        partial = client.get(path + '?inline=true', headers={**headers, 'Range': 'bytes=2-9'})
        assert partial.status_code == 206 and partial.content == body[2:10]
        assert partial.headers['content-range'] == f'bytes 2-9/{len(body)}'
        assert_inert_headers(partial)


def test_derived_raster_preview_is_safe_and_does_not_modify_original(client, files_project):
    _project, headers, upload = files_project
    stream = io.BytesIO(); Image.new('RGB', (800, 400), 'navy').save(stream, format='PNG')
    body = stream.getvalue()
    attachment = upload('real.png', body, 'image/png')
    for path in paths(attachment):
        for size, expected in [('thumb', (360, 180)), ('preview', (800, 400))]:
            response = client.get(path, headers=headers, params={'inline': 'true', 'size': size})
            assert response.status_code == 200 and response.headers['content-type'] == 'image/webp'
            assert response.headers['content-disposition'].startswith('inline;')
            assert_inert_headers(response)
            with Image.open(io.BytesIO(response.content)) as image:
                assert image.size == expected
        assert client.get(path, headers=headers).content == body


def test_private_media_keeps_authentication_and_project_scope(client, repo, files_project):
    _project, headers, upload = files_project
    attachment = upload('brief.txt', b'private fixture', 'text/plain')
    leaf, api = paths(attachment)
    client.cookies.clear()
    assert client.get(leaf, follow_redirects=False).status_code == 303
    assert client.get(api).status_code == 401
    stranger = repo.create_device(label='private-media-stranger')
    token = repo.create_api_token(device_id=stranger['id'], name='stranger')
    assert client.get(api, headers={'Authorization': f'Bearer {token.raw}'}).status_code == 403
    repo.create_project(slug='another-media-project', name='Other', created_by_device_id=stranger['id'])
    wrong = api.replace('private-media-test', 'another-media-project')
    assert client.get(wrong, headers={'Authorization': f'Bearer {token.raw}'}).status_code == 404
    login(client)
    assert client.get(wrong.removeprefix('/api')).status_code == 404


def test_browser_images_video_and_active_document_download_remain_usable(client, files_project, live_hub, real_video):
    playwright = pytest.importorskip('playwright.sync_api')
    _project, _headers, upload = files_project
    stream = io.BytesIO(); Image.new('RGB', (32, 24), 'navy').save(stream, format='PNG')
    image = upload('real.png', stream.getvalue(), 'image/png')
    video = upload('real.mp4', real_video, 'video/mp4')
    active = upload('pretend.png', b'<script>opener.attachmentCodeExecuted=true</script>', 'text/html')
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            context = browser.new_context(accept_downloads=True)
            logged = context.request.post(live_hub + '/login', form={'username': 'owner', 'password': 'owner-password'})
            assert logged.ok
            page = context.new_page()
            page.goto(live_hub + '/projects/private-media-test')
            page.set_content('<img id="image"><video id="video" preload="metadata" controls></video>'
                             '<a id="download">下载原件</a>')
            page.locator('#image').evaluate('(el, url) => el.src=url', live_hub + paths(image)[0] + '?inline=true&size=thumb')
            page.locator('#video').evaluate('(el, url) => el.src=url', live_hub + paths(video)[0] + '?inline=true')
            page.wait_for_function("document.querySelector('#image').naturalWidth===32")
            page.wait_for_function("document.querySelector('#video').readyState>=1 && document.querySelector('#video').videoWidth===32")
            page.locator('#download').evaluate('(el,url)=>el.href=url', live_hub + paths(active)[0] + '?inline=true')
            with page.expect_download() as pending:
                page.locator('#download').click()
            download = pending.value
            assert download.suggested_filename == 'pretend.png'
            assert download.failure() is None
            assert page.evaluate('window.attachmentCodeExecuted !== true')
            assert '/projects/private-media-test' in page.url
        finally:
            browser.close()
