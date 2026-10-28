from types import SimpleNamespace
from unittest.mock import Mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from codex_hub.execution_leases import ExecutionStore
from codex_bridge import desktop_window as desktop
from test_execution_leases import lease_task, start, RESULT
from test_work_delivery import upload_result
from test_workspace_shell import login
from test_desktop_window import host
from test_reading_browser import page, STATIC_DIR


def test_published_download_and_preview_have_distinct_response_dispositions(client, repo, lease_task):
    data = lease_task
    run = start(ExecutionStore(repo), data)
    uploaded = upload_result(client, repo, data, run, name='final.png', body=b'original-image-bytes', reference='output/final.png')
    attachment_id = uploaded.json()['attachment_id']
    body = '[下载](output/final.png) · [预览](output/final.png)'
    ExecutionStore(repo).complete(data['task']['id'], data['device']['id'], run['generation'], run['run_key'],
                                **{**RESULT, 'body':body, 'summary':body})
    summary = repo.get_task(data['task']['id'])['result_summary']
    assert f'[下载](../attachments/{attachment_id})' in summary
    assert f'[预览](../attachments/{attachment_id}?inline=true)' in summary
    login(client)
    direct = client.get(uploaded.json()['url'])
    preview = client.get(uploaded.json()['url']+'?inline=true')
    assert direct.headers['content-disposition'].startswith('attachment;')
    assert preview.headers['content-disposition'].startswith('inline;')
    assert direct.content == preview.content == b'original-image-bytes'


@pytest.fixture()
def download_server():
    payload = b'original binary file'
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            is_file = '/attachments/' in self.path
            content = payload if is_file else ('<textarea id="draft">unfinished chat</textarea>'
                '<div data-markdown-source>[下载](../attachments/1196?inline=true&amp;size=preview)</div>').encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream' if is_file else 'text/html; charset=utf-8')
            if is_file:
                self.send_header('Content-Disposition', 'attachment; filename="final.png"')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize('base', ['/hub/projects/demo', '/hub/projects/demo/conversations/6'])
def test_historical_download_link_downloads_original_and_preserves_chat(page, tmp_path, base, download_server):
    origin, payload = download_server
    page.goto(origin+base)
    page.add_script_tag(path=str(STATIC_DIR/'codex-markdown.js'))
    link = page.get_by_role('link', name='下载')
    assert link.get_attribute('href') == origin+'/hub/projects/demo/attachments/1196'
    assert link.get_attribute('download') == '' and link.get_attribute('target') is None
    with page.expect_download() as captured:
        link.click()
    download = captured.value
    output = tmp_path/download.suggested_filename
    download.save_as(output)
    assert output.read_bytes() == payload
    assert page.url == origin+base
    assert page.locator('#draft').input_value() == 'unfinished chat'


def test_attachment_preview_opens_separately_without_replacing_chat(page):
    page.set_content('<base href="https://hub.test/hub/projects/demo/conversations/6"><div data-markdown-source>'
                     '[预览](../attachments/1197?inline=true) [外部下载](https://other.test/attachments/1?inline=true)</div>')
    page.add_script_tag(path=str(STATIC_DIR/'codex-markdown.js'))
    preview = page.get_by_role('link', name='预览')
    assert preview.get_attribute('target') == '_blank'
    assert preview.get_attribute('href') == 'https://hub.test/hub/projects/demo/attachments/1197?inline=true'
    external = page.get_by_role('link', name='外部下载')
    assert external.get_attribute('download') is None
    assert external.get_attribute('href').endswith('?inline=true')


def test_cancelled_and_repeated_native_navigation_never_trigger_recovery(host):
    host.restore_workspace = Mock()
    host._local_page = False
    host._active_navigation = 10
    host.on_navigation_completed(None, SimpleNamespace(NavigationId=10, IsSuccess=False, WebErrorStatus='OperationCanceled'))
    host.on_download_starting(None, None)
    for _ in range(3):
        host.on_navigation_completed(None, SimpleNamespace(NavigationId=10, IsSuccess=False))
    host.restore_workspace.assert_not_called()
    host.window.load_html.assert_not_called()


def test_failed_cache_restore_stops_after_one_attempt_and_manual_retry_resets(host):
    called = threading.Event()
    host.restore_workspace = Mock(side_effect=lambda *args: called.set())
    target = host.location.home
    host.on_navigation(None, SimpleNamespace(Uri=target, NavigationId=11, Cancel=False))
    host.on_navigation_completed(None, SimpleNamespace(NavigationId=11, IsSuccess=False))
    assert called.wait(2)
    host.on_navigation_completed(None, SimpleNamespace(NavigationId=11, IsSuccess=False))
    host.on_navigation(None, SimpleNamespace(Uri=target, NavigationId=12, Cancel=False))
    host.on_navigation_completed(None, SimpleNamespace(NavigationId=12, IsSuccess=False))
    assert host.restore_workspace.call_count == 1
    assert host.window.load_html.call_count == 1
    assert host._local_page
    host.on_navigation_completed(None, SimpleNamespace(NavigationId=12, IsSuccess=False))
    assert host.window.load_html.call_count == 1
    host.navigate_home()
    assert host._recovery_target is None
