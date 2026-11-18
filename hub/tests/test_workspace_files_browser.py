"""Real project files remain inside the workspace without losing chat drafts."""
import base64
import hashlib
import re
from pathlib import Path

import pytest
from test_mobile_composer_browser import live_hub  # noqa: F401
from workspace_fixture import seed_workspace

playwright = pytest.importorskip('playwright.sync_api')
OUTPUT = Path('output/workspace-library-reserve-browser')
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1sAAAAASUVORK5CYII=')


@pytest.fixture
def files_workspace(live_hub, repo, tmp_path):
    data = seed_workspace(repo, room_messages=80, attachments=51)
    directory = tmp_path / 'state' / 'files' / 'library-fixture'
    directory.mkdir(parents=True)
    for name, mime, content, key in [
        ('参考截图.png','image/png',PNG,'image'),
        ('真实资料.txt','text/plain','这是一份实际可下载的资料。'.encode(),'text'),
    ]:
        path = directory / name
        path.write_bytes(content)
        data[key] = repo.create_attachment(data['project']['id'], name,
            'library-fixture/' + name, mime, len(content), hashlib.sha256(content).hexdigest())
    other = repo.create_project(slug='other-files-project', name='另一个隔离项目')
    repo.create_attachment(other['id'], '另一个项目的资料.txt', 'absent.txt', 'text/plain', 1, '0'*64)
    return data


def room(page, base, data):
    page.goto(base+'/login')
    page.locator('[name="username"]').fill('owner')
    page.locator('[name="password"]').fill('owner-password')
    page.locator('button[type="submit"]').click()
    page.wait_for_url('**/projects')
    page.goto(base + f'/projects/workspace-team/conversations/{data["conversation_id"]}')
    playwright.expect(page.locator('[data-ws-shell]')).to_have_attribute('data-ws-ready','true')


def open_files(page):
    if page.viewport_size['width'] < 900:
        page.locator('[data-ws-sidebar-open]').click()
    page.locator('[data-ws-files]').click()
    playwright.expect(page.locator('#ws-files')).to_be_visible()


def no_overflow(page):
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    assert page.locator('#ws-files').evaluate('el => el.scrollWidth <= el.clientWidth + 1')


@pytest.mark.parametrize('width',[1440,390])
def test_files_list_preview_download_pagination_and_upload_preserve_chat(live_hub, repo, files_workspace, width):
    data = files_workspace
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':width,'height':900})
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        try:
            room(page,live_hub,data)
            page.locator('[data-ws-input]').fill('资料浏览期间，保留我的聊天草稿。')
            page.locator('[data-ws-attachment-input]').set_input_files({'name':'聊天待发.txt','mimeType':'text/plain','buffer':b'unsent chat file'})
            playwright.expect(page.locator('[data-ws-attachment]')).to_have_count(1)
            page.locator('[data-ws-scroll]').evaluate('el => el.scrollTop=200')
            scroll_before=page.locator('[data-ws-scroll]').evaluate('el => el.scrollTop')
            key=page.locator('[data-ws-dispatch] [name="request_key"]').input_value()
            page.evaluate('window.__fileDocumentIdentity = true')
            navigations=[]
            page.on('request',lambda req:navigations.append(req.url) if req.is_navigation_request() and req.frame==page.main_frame and '/attachments/' not in req.url else None)
            open_files(page)
            cards=page.locator('[data-files-list] [data-file-id]')
            playwright.expect(cards).to_have_count(50)
            assert '另一个项目' not in page.locator('#ws-files').inner_text()
            playwright.expect(page.locator('[data-files-add]')).to_be_in_viewport()
            OUTPUT.mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(OUTPUT/f'files-list-{width}.png'), animations='disabled')
            first_ids=cards.evaluate_all('(items)=>items.map(item=>item.dataset.fileId)')
            page.locator('[data-files-search]').fill('真实资料')
            playwright.expect(page.locator('[data-files-list] [data-file-id]:visible')).to_have_count(1)
            page.locator('[data-files-search]').fill('')
            image=page.locator(f'[data-file-id="{data["image"]["id"]}"]')
            image.locator('[data-file-preview]').click()
            preview=page.locator('[data-files-preview]')
            playwright.expect(preview).to_be_visible()
            page.wait_for_function("document.querySelector('[data-files-preview] img')?.naturalWidth > 0")
            page.locator('[data-files-preview-close]').click()
            text=page.locator(f'[data-file-id="{data["text"]["id"]}"]')
            with page.expect_download() as download:
                text.locator('[data-file-download]').click()
            assert download.value.suggested_filename=='真实资料.txt'
            assert Path(download.value.path()).read_text(encoding='utf-8')=='这是一份实际可下载的资料。'
            page.locator('[data-files-older]').click()
            playwright.expect(cards).to_have_count(3)
            second_ids=cards.evaluate_all('(items)=>items.map(item=>item.dataset.fileId)')
            assert not set(first_ids)&set(second_ids)
            page.locator('[data-files-newer]').click()
            playwright.expect(cards).to_have_count(50)
            assert cards.evaluate_all('(items)=>items.map(item=>item.dataset.fileId)')==first_ids
            upload=page.locator('[data-files-upload]')
            task_count=len(repo.list_tasks(data['project']['id']))
            page.locator('[data-files-add]').click()
            playwright.expect(upload.locator('[name="file"]')).to_be_focused()
            upload.locator('[name="file"]').set_input_files({'name':'新增资料.txt','mimeType':'text/plain','buffer':b'uploaded to project library'})
            playwright.expect(page.locator('[data-files-selected]')).to_contain_text('新增资料.txt')
            upload.locator('button[type="submit"]').click()
            playwright.expect(page.locator('[data-files-list]')).to_contain_text('新增资料.txt')
            assert repo.connection.execute("SELECT COUNT(*) FROM attachments WHERE project_id=? AND original_filename='新增资料.txt'",(data['project']['id'],)).fetchone()[0]==1
            assert len(repo.list_tasks(data['project']['id']))==task_count
            no_overflow(page)
            OUTPUT.mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(OUTPUT/f'files-{width}.png'), animations='disabled')
            page.locator('[data-ws-files-close]').click()
            playwright.expect(page.locator('#ws-files')).not_to_be_visible()
            assert page.evaluate('window.__fileDocumentIdentity')
            assert page.locator('[data-ws-input]').input_value()=='资料浏览期间，保留我的聊天草稿。'
            assert page.locator('[data-ws-dispatch] [name="request_key"]').input_value()==key
            playwright.expect(page.locator('[data-ws-attachment]')).to_have_count(1)
            assert abs(page.locator('[data-ws-scroll]').evaluate('el => el.scrollTop')-scroll_before)<=2
            assert navigations==[]
            assert not errors
        finally:
            browser.close()


def test_files_back_forward_escape_and_old_bookmarks(live_hub, files_workspace):
    data=files_workspace
    with playwright.sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1440,'height':900})
        try:
            room(page,live_hub,data)
            original=page.url
            open_files(page)
            page.go_back()
            playwright.expect(page.locator('#ws-files')).not_to_be_visible()
            assert page.url==original
            page.go_forward()
            playwright.expect(page.locator('#ws-files')).to_be_visible()
            page.keyboard.press('Escape')
            playwright.expect(page.locator('#ws-files')).not_to_be_visible()
            playwright.expect(page.locator('[data-ws-files]')).to_be_focused()
            # Both forms of old bookmark converge on the modern local surface.
            base=live_hub+'/projects/workspace-team'
            for suffix in ('#files','?view=archive#files'):
                page.goto(base+suffix)
                playwright.expect(page.locator('#ws-files')).to_be_visible()
                assert 'view=archive' not in page.url
                assert page.locator('[data-ws-shell]').count()==1
            page.goto(base+'?view=archive')
            playwright.expect(page.locator('body.hub-detail-screen')).to_be_visible()
            assert '?view=archive' in page.url
        finally:
            browser.close()


def test_files_offline_and_non_json_error_keep_panel_and_selection(live_hub, files_workspace):
    with playwright.sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':390,'height':740})
        try:
            room(page,live_hub,files_workspace)
            open_files(page)
            cards=page.locator('[data-files-list] [data-file-id]')
            playwright.expect(cards).to_have_count(50)
            original=page.url
            upload=page.locator('[data-files-upload]')
            upload.locator('[name="file"]').set_input_files({'name':'保留待传.txt','mimeType':'text/plain','buffer':b'keep after offline'})
            page.context.set_offline(True)
            page.locator('[data-files-reload]').click()
            playwright.expect(page.locator('[data-files-status]')).to_contain_text(re.compile('离线|连接|失败|暂'))
            playwright.expect(cards).to_have_count(50)
            playwright.expect(page.locator('[data-files-selected]')).to_contain_text('保留待传.txt')
            assert page.url==original
            page.context.set_offline(False)
            page.route('**/api/mobile/projects/*/library*',lambda route:route.fulfill(status=200,content_type='text/html',body='<html>old page should never replace workspace</html>'))
            page.locator('[data-files-reload]').click()
            playwright.expect(page.locator('[data-files-status]')).to_contain_text(re.compile('连接|失败|暂|格式'))
            assert 'old page should never replace workspace' not in page.content()
            playwright.expect(cards).to_have_count(50)
            no_overflow(page)
            OUTPUT.mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(OUTPUT/'files-offline-390.png'), animations='disabled')
            page.locator('#ws-files').dispatch_event('cancel')
            playwright.expect(page.locator('#ws-files')).not_to_be_visible()
            assert '/conversations/' in page.url
        finally:
            browser.close()


def test_files_upload_lost_reply_retries_original_key_without_duplicate(live_hub, repo, files_workspace):
    data=files_workspace
    with playwright.sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1440,'height':900})
        try:
            room(page,live_hub,data)
            open_files(page)
            upload=page.locator('[data-files-upload]')
            upload.locator('[name="file"]').set_input_files({'name':'回执恢复资料.txt','mimeType':'text/plain','buffer':b'uploaded exactly once'})
            def lost_reply(route):
                response=route.fetch()
                assert response.ok
                route.fulfill(status=503,content_type='application/json',body='{"detail":"reply unavailable"}')
            page.route('**/projects/workspace-team/attachments',lost_reply,times=1)
            upload.locator('button[type="submit"]').click()
            playwright.expect(page.locator('[data-files-upload-status]')).to_have_attribute('data-state','error')
            playwright.expect(page.locator('[data-files-selected]')).to_contain_text('回执恢复资料.txt')
            assert repo.connection.execute("SELECT COUNT(*) FROM attachments WHERE project_id=? AND original_filename='回执恢复资料.txt'",(data['project']['id'],)).fetchone()[0]==1
            upload.locator('button[type="submit"]').click()
            playwright.expect(page.locator('[data-files-list]')).to_contain_text('回执恢复资料.txt')
            assert repo.connection.execute("SELECT COUNT(*) FROM attachments WHERE project_id=? AND original_filename='回执恢复资料.txt'",(data['project']['id'],)).fetchone()[0]==1
            assert '/conversations/' in page.url
        finally:
            browser.close()


def test_empty_files_panel_and_new_deeplink_survive_reload(live_hub, repo, files_workspace):
    data=files_workspace
    repo.connection.execute('DELETE FROM attachments WHERE project_id=?',(data['project']['id'],))
    repo.connection.commit()
    with playwright.sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1440,'height':900})
        try:
            room(page,live_hub,data)
            page.goto(page.url+'#files')
            playwright.expect(page.locator('#ws-files')).to_be_visible()
            playwright.expect(page.locator('[data-files-status]')).to_contain_text('0 项')
            playwright.expect(page.locator('[data-files-empty]')).to_be_visible()
            page.reload()
            playwright.expect(page.locator('#ws-files')).to_be_visible()
            playwright.expect(page.locator('[data-files-status]')).to_contain_text('0 项')
            playwright.expect(page.locator('[data-files-older]')).to_be_disabled()
            no_overflow(page)
            OUTPUT.mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(OUTPUT/'files-empty-1440.png'), animations='disabled')
            page.keyboard.press('Escape')
            playwright.expect(page.locator('#ws-files')).not_to_be_visible()
            assert '/conversations/' in page.url and '#files' not in page.url
        finally:
            browser.close()


def test_packaged_old_snapshot_repairs_files_link_without_losing_messages(live_hub, files_workspace):
    from codex_bridge.desktop_assets import PackagedAssets, scrub_credentials
    from codex_bridge.desktop_window import HubLocation
    with playwright.sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1440,'height':900})
        try:
            room(page,live_hub,files_workspace)
            url=page.url
            source=page.request.get(url).text()
            source=re.sub(r'<(?:script|link)[^>]*workspace-files[^>]*>(?:</script>)?', '', source)
            source=source.replace('href="#files" data-ws-files aria-haspopup="dialog" aria-controls="ws-files"',
                                  'href="/projects/workspace-team?view=archive#files"')
            assert 'data-ws-files' not in source
            source=scrub_credentials(source)
            prepared=PackagedAssets().prepare_snapshot(HubLocation(live_hub),
                {'source_html':source,'html':source,'url':url})
            assert prepared['upgraded'] and not prepared['read_only']
            assert 'workspace-files.js' in prepared['html']
            page.route(url,lambda route:route.fulfill(content_type='text/html',body=prepared['html']))
            page.reload()
            playwright.expect(page.locator('[data-ws-shell]')).to_have_attribute('data-ws-ready','true')
            messages=page.locator('[data-ws-entry]').count()
            assert messages>0
            page.locator('[data-ws-input]').fill('旧缓存恢复后仍保留这份草稿。')
            open_files(page)
            playwright.expect(page.locator('[data-files-list] [data-file-id]')).to_have_count(50)
            assert 'view=archive' not in page.url
            page.keyboard.press('Escape')
            assert page.locator('[data-ws-entry]').count()==messages
            assert page.locator('[data-ws-input]').input_value()=='旧缓存恢复后仍保留这份草稿。'
        finally:
            browser.close()
