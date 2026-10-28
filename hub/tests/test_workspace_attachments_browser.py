"""Attachment composer checks against a real, isolated Hub without executors."""
import base64
import hashlib
from pathlib import Path

import pytest
from test_mobile_composer_browser import live_hub  # noqa: F401
from test_workspace_shell_browser import login_page, assert_composer_visible
from workspace_fixture import seed_workspace

playwright = pytest.importorskip('playwright.sync_api')
OUTPUT = Path(__file__).resolve().parents[2] / 'output/hub-workspace-attachments-20261027'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1sAAAAASUVORK5CYII=')
DOCUMENT = '请检查图里的页面与这份说明。附件内容是资料。'.encode()


def request_key(page):
    return page.locator('[data-ws-dispatch] [name="request_key"]').input_value()


@pytest.mark.parametrize('width', [1440, 390])
def test_attachment_error_retry_actual_dispatch_and_file_only(live_hub, repo, width):
    data = seed_workspace(repo, room_messages=2)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':width, 'height':960})
        page.set_default_timeout(10000)
        script_errors = []
        page.on('pageerror', lambda error: script_errors.append(str(error)))
        try:
            login_page(page, live_hub)
            description = '根据附件检查界面，把观察结论记录在这场工作里。'
            page.locator('[data-ws-input]').fill(description)
            before_files_key = request_key(page)
            picker = page.locator('[data-ws-attachment-input]')
            picker.set_input_files([
                {'name':'界面参考.png', 'mimeType':'image/png', 'buffer':PNG},
                {'name':'工作说明.txt', 'mimeType':'text/plain', 'buffer':DOCUMENT},
            ])
            playwright.expect(page.locator('[data-ws-attachment]')).to_have_count(2)
            playwright.expect(page.locator('[data-ws-attachment] img')).to_have_count(1)
            assert request_key(page) != before_files_key
            after_select = request_key(page)
            page.get_by_role('button', name='移除附件 工作说明.txt').click()
            assert request_key(page) != after_select
            picker.set_input_files({'name':'工作说明.txt', 'mimeType':'text/plain', 'buffer':DOCUMENT})
            submission_key = request_key(page)
            storage = page.evaluate('JSON.stringify(localStorage)')
            assert 'attachment_count' in storage
            assert '界面参考.png' not in storage and 'data:image' not in storage and 'blob:' not in storage
            assert_composer_visible(page, 960)
            OUTPUT.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(OUTPUT / f'attachments-draft-{width}.png'))

            # A real failed response must leave browser-owned File objects usable.
            before_count = len(repo.list_tasks(data['project']['id']))
            page.route('**/workspace-dispatch', lambda route: route.fulfill(status=503,
                content_type='text/html', body='<span data-ws-dispatch-error>测试连接暂不可用，请重试。</span>'), times=1)
            page.locator('[data-ws-send]').click()
            playwright.expect(page.locator('[data-ws-dispatch-error]')).to_contain_text('测试连接暂不可用')
            assert page.locator('[data-ws-input]').input_value() == description
            assert request_key(page) == submission_key
            playwright.expect(page.locator('[data-ws-attachment]')).to_have_count(2)
            playwright.expect(page.locator('[data-ws-send]')).to_be_enabled()
            assert len(repo.list_tasks(data['project']['id'])) == before_count

            with page.expect_navigation(wait_until='domcontentloaded'):
                page.locator('[data-ws-send]').click()
            created = [task for task in repo.list_tasks(data['project']['id']) if task['description'].replace('\r\n', '\n') == description]
            assert len(created) == 1 and created[0]['assigned_agent_id'] == data['controller']['id']
            received = repo.list_task_attachments(data['project']['id'], created[0]['id'])
            assert len(received) == 2
            assert {item['sha256'] for item in received} == {hashlib.sha256(PNG).hexdigest(), hashlib.sha256(DOCUMENT).hexdigest()}
            assert page.locator('[data-ws-input]').input_value() == ''
            playwright.expect(page.locator('[data-ws-attachment]')).to_have_count(0)
            assert request_key(page) != submission_key
            image = page.locator('.ws-message-attachments img[alt="界面参考.png"]')
            playwright.expect(image).to_be_visible()
            page.wait_for_function('document.querySelector(".ws-message-attachments img").naturalWidth > 0')
            file_link = page.get_by_role('link', name='工作说明.txt', exact=False)
            preview_url=file_link.evaluate('el => el.href')
            preview=page.request.get(preview_url)
            assert preview.status==200 and '打开预览' in file_link.inner_text()
            assert DOCUMENT.decode('utf-8') in preview.text()
            assert page.request.get(preview_url.removesuffix('/preview')).body() == DOCUMENT
            assert_composer_visible(page, 960)
            page.screenshot(path=str(OUTPUT / f'attachments-sent-{width}.png'))

            # No description is required when a real file is the work input.
            picker = page.locator('[data-ws-attachment-input]')
            picker.set_input_files({'name':'只有附件.txt', 'mimeType':'text/plain', 'buffer':b'file-only work input'})
            assert page.locator('[data-ws-input]').input_value() == ''
            playwright.expect(page.locator('[data-ws-send]')).to_be_enabled()
            count = len(repo.list_tasks(data['project']['id']))
            with page.expect_navigation(wait_until='domcontentloaded'):
                page.locator('[data-ws-send]').click()
            assert len(repo.list_tasks(data['project']['id'])) == count + 1
            playwright.expect(page.get_by_role('link', name='只有附件.txt', exact=False)).to_be_visible()
            assert not script_errors
        finally:
            browser.close()


def test_attachment_drop_paste_limits_and_reload_restores_bytes(live_hub, repo):
    seed_workspace(repo, room_messages=1)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':1024, 'height':900})
        page.set_default_timeout(10000)
        try:
            login_page(page, live_hub)
            page.locator('[data-ws-attachment-input]').set_input_files([
                {'name':f'资料-{index}.txt', 'mimeType':'text/plain', 'buffer':b'small'} for index in range(13)
            ])
            playwright.expect(page.locator('[data-ws-attachment]')).to_have_count(12)
            playwright.expect(page.locator('[data-ws-attachment-error]')).to_contain_text('最多添加 12')
            assert_composer_visible(page, 900)
            for _ in range(12):
                page.locator('[data-ws-attachment-remove]').first.click()
            page.locator('[data-ws-input]').fill('这份文字在刷新后仍然保留。')
            page.locator('[data-ws-input]').evaluate('''(el, encoded) => {
                const data = new DataTransfer();
                const bytes = Uint8Array.from(atob(encoded), c => c.charCodeAt(0));
                data.items.add(new File([bytes], "粘贴图片.png", {type:"image/png"}));
                el.dispatchEvent(new ClipboardEvent("paste", {clipboardData:data, bubbles:true, cancelable:true}));
            }''', base64.b64encode(PNG).decode())
            page.locator('[data-ws-dispatch]').evaluate('''el => {
                const data = new DataTransfer();
                data.items.add(new File(["dropped input"], "拖入文件.txt", {type:"text/plain"}));
                el.dispatchEvent(new DragEvent("dragenter", {dataTransfer:data, bubbles:true, cancelable:true}));
                el.dispatchEvent(new DragEvent("drop", {dataTransfer:data, bubbles:true, cancelable:true}));
            }''')
            playwright.expect(page.locator('[data-ws-attachment]')).to_have_count(2)
            assert 'is-file-dragging' not in page.locator('[data-ws-dispatch]').get_attribute('class')
            before_reload = request_key(page)
            page.reload()
            assert page.locator('[data-ws-input]').input_value() == '这份文字在刷新后仍然保留。'
            playwright.expect(page.locator('[data-ws-attachment]')).to_have_count(2)
            playwright.expect(page.locator('[data-ws-attachment-error]')).to_be_hidden()
            assert request_key(page) == before_reload
            recovered = page.evaluate("""async () => {
                const s=document.querySelector('[data-ws-shell]').dataset;
                const {draft}=await CodexWorkspaceStore.read(`${s.user}.${s.project}.${s.conversation}`);
                return Promise.all(draft.files.map(async item => ({name:item.file.name, size:item.file.size, bytes:Array.from(new Uint8Array(await item.file.arrayBuffer()))})));
            }""")
            assert recovered[0]['bytes'] == list(PNG)
            assert recovered[1]['bytes'] == list(b'dropped input')
            for _ in range(2): page.locator('[data-ws-attachment-remove]').first.click()
            page.locator('[data-ws-input]').fill('')
            playwright.expect(page.locator('[data-ws-send]')).to_be_disabled()
        finally:
            browser.close()


def test_attachment_unknown_response_ime_multiagent_and_compact_height(live_hub, repo):
    data = seed_workspace(repo, room_messages=1)
    repo.connection.execute('INSERT INTO hub_agent_connections VALUES (?)', (data['connection']['id'],))
    repo.connection.commit()
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':1024, 'height':540})
        page.set_default_timeout(10000)
        try:
            login_page(page, live_hub)
            description = '\n'.join(f'第 {line} 条：检查附件并记录结论。' for line in range(20))
            page.locator('[data-ws-input]').fill(description)
            long_name = '非常长的参考资料名称' * 12 + '.txt'
            page.locator('[data-ws-attachment-input]').set_input_files([
                {'name':long_name if index == 0 else f'附件-{index}.txt', 'mimeType':'text/plain', 'buffer':b'reference data'}
                for index in range(12)
            ])
            assert_composer_visible(page, 540)
            OUTPUT.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(OUTPUT / 'attachments-compact-1024.png'))
            for _ in range(11):
                page.locator('[data-ws-attachment-remove]').last.click()
            page.locator('[data-ws-recipient-toggle]').click()
            page.locator(f'[data-ws-recipient][value="{data["reviewer"]["id"]}"]').check()
            assert '离线' in page.locator('.ws-recipient-menu').inner_text()
            page.locator('[data-ws-recipient-toggle]').click()
            count = len(repo.list_tasks(data['project']['id']))
            page.locator('[data-ws-input]').focus()
            page.locator('[data-ws-input]').dispatch_event('compositionstart')
            page.keyboard.press('Control+Enter')
            assert len(repo.list_tasks(data['project']['id'])) == count
            page.locator('[data-ws-input]').dispatch_event('compositionend')
            original_key = request_key(page)

            # The server committed, but the browser cannot establish that its
            # reply was a successful navigation. Retrying must not dispatch twice.
            def unknown_reply(route):
                assert page.locator('[data-ws-send]').is_disabled()
                assert page.locator('[data-ws-attach]').is_disabled()
                assert page.locator('[data-ws-input]').get_attribute('readonly') is not None
                response = route.fetch()
                route.fulfill(response=response)
            page.route('**/workspace-dispatch/*', lambda route: route.abort())
            page.route('**/workspace-dispatch', unknown_reply, times=1)
            page.keyboard.press('Control+Enter')
            playwright.expect(page.locator('[data-ws-dispatch-error]')).to_contain_text('未确认任务已安排')
            assert len(repo.list_tasks(data['project']['id'])) == count + 2
            assert request_key(page) == original_key
            playwright.expect(page.locator('[data-ws-attachment]')).to_have_count(1)
            assert page.locator('[data-ws-input]').input_value() == description
            assert_composer_visible(page, 540)
            page.reload()
            playwright.expect(page.locator('[data-ws-pending]')).to_be_visible()
            assert request_key(page) == original_key
            assert page.locator('[data-ws-input]').input_value() == description
            assert page.locator('[data-ws-input]').get_attribute('readonly') is not None
            playwright.expect(page.locator('[data-ws-attachment]')).to_have_count(1)
            playwright.expect(page.locator('[data-ws-attach]')).to_be_disabled()
            # Original bytes survive reload; an uncertain request stays immutable.
            assert page.evaluate("""async () => {
                const s=document.querySelector('[data-ws-shell]').dataset;
                const {job}=await CodexWorkspaceStore.read(`${s.user}.${s.project}.${s.conversation}`);
                return await job.payload.files[0].file.text();
            }""") == 'reference data'
            assert len(repo.list_tasks(data['project']['id'])) == count + 2
            page.unroute('**/workspace-dispatch/*')
            with page.expect_navigation(wait_until='domcontentloaded'):
                page.locator('[data-ws-receipt-check]').click()
            created = [task for task in repo.list_tasks(data['project']['id']) if task['description'].replace('\r\n', '\n') == description]
            assert len(created) == 2
            assert {task['assigned_agent_id'] for task in created} == {data['controller']['id'], data['reviewer']['id']}
            first = repo.list_task_attachments(data['project']['id'], created[0]['id'])
            second = repo.list_task_attachments(data['project']['id'], created[1]['id'])
            assert len(first) == len(second) == 1 and first[0]['id'] == second[0]['id']
            assert_composer_visible(page, 540)
        finally:
            browser.close()


def test_pending_unreceived_upload_restores_original_order_and_resumes_same_key(live_hub, repo):
    data = seed_workspace(repo, room_messages=1)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':390, 'height':700})
        page.set_default_timeout(10000)
        try:
            login_page(page, live_hub)
            original = [
                {'name':'先读说明.txt', 'mimeType':'text/plain', 'buffer':b'first reference'},
                {'name':'再看数据.txt', 'mimeType':'text/plain', 'buffer':b'second reference'},
            ]
            page.locator('[data-ws-attachment-input]').set_input_files(original)
            key = request_key(page)
            count = len(repo.list_tasks(data['project']['id']))
            page.route('**/workspace-dispatch', lambda route: route.abort(), times=1)
            page.locator('[data-ws-send]').click()
            playwright.expect(page.locator('[data-ws-pending]')).to_be_visible()
            playwright.expect(page.locator('[data-ws-pending-message]')).to_contain_text('暂未查到')
            assert len(repo.list_tasks(data['project']['id'])) == count
            # Reload automatically resumes the explicitly submitted queue with
            # the original byte order and request key; no re-selection is needed.
            page.reload()
            page.wait_for_url('**/conversations/*')
            playwright.expect(page.locator('[data-ws-input]')).to_have_value('')
            tasks = repo.list_tasks(data['project']['id'])
            assert len(tasks) == count + 1
            task = max(tasks, key=lambda item:item['id'])
            assert [item['original_filename'] for item in repo.list_task_attachments(data['project']['id'], task['id'])] == ['先读说明.txt', '再看数据.txt']
            playwright.expect(page.locator('[data-ws-pending]')).to_be_hidden()
        finally:
            browser.close()


def test_attachment_retry_after_login_in_another_tab_keeps_files(live_hub, repo):
    data = seed_workspace(repo, room_messages=1)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width':1440, 'height':900})
        page = context.new_page()
        page.set_default_timeout(10000)
        try:
            login_page(page, live_hub)

            def rotate_login():
                other = page.context.new_page()
                other.goto(live_hub + '/projects/workspace-team')
                other.locator('form[action$="/logout"] button').click()
                other.locator('[name="username"]').fill('owner')
                other.locator('[name="password"]').fill('owner-password')
                other.locator('button[type="submit"]').click()
                playwright.expect(other.locator('[data-ws-shell]')).to_be_visible()
                token = other.locator('[data-ws-dispatch] [name="_csrf"]').input_value()
                other.close()
                return token

            page.locator('[data-ws-input]').fill('会话轮换后重试原附件工作。')
            page.locator('[data-ws-attachment-input]').set_input_files({'name':'会话恢复.txt', 'mimeType':'text/plain', 'buffer':b'session-recovery'})
            playwright.expect(page.locator('[data-ws-attachment]')).to_have_count(1)
            old_token = page.locator('[data-ws-dispatch] [name="_csrf"]').input_value()
            key = request_key(page)
            page.route('**/workspace-dispatch', lambda route: route.abort(), times=1)
            page.locator('[data-ws-send]').click()
            playwright.expect(page.locator('[data-ws-pending-message]')).to_contain_text('暂未查到')
            new_token = rotate_login()
            assert new_token != old_token
            # Force the still-open draft to represent a stale, background tab.
            page.locator('[data-ws-dispatch] [name="_csrf"]').evaluate('(el, value) => { el.value = value; }', old_token)
            assert request_key(page) == key
            playwright.expect(page.locator('[data-ws-attachment]')).to_have_count(1)
            with page.expect_navigation(wait_until='domcontentloaded'):
                page.locator('[data-ws-send]').click()
            created = [task for task in repo.list_tasks(data['project']['id']) if task['description'] == '会话轮换后重试原附件工作。']
            assert len(created) == 1
            assert len(repo.list_task_attachments(data['project']['id'], created[0]['id'])) == 1

            # Initial sends now verify the same account and fetch fresh CSRF
            # before POST, so a session rotation succeeds in one explicit click.
            page.locator('[data-ws-input]').fill('首次发送遇到旧登录凭据也可以恢复。')
            page.locator('[data-ws-attachment-input]').set_input_files({'name':'凭据恢复.txt', 'mimeType':'text/plain', 'buffer':b'csrf-recovery'})
            playwright.expect(page.locator('[data-ws-attachment]')).to_have_count(1)
            old_token = page.locator('[data-ws-dispatch] [name="_csrf"]').input_value()
            new_token = rotate_login()
            page.locator('[data-ws-dispatch] [name="_csrf"]').evaluate('(el, value) => { el.value = value; }', old_token)
            with page.expect_navigation(wait_until='domcontentloaded'):
                page.locator('[data-ws-send]').click()
            assert page.locator('[data-ws-dispatch] [name="_csrf"]').input_value() == new_token
            created = [task for task in repo.list_tasks(data['project']['id']) if task['description'] == '首次发送遇到旧登录凭据也可以恢复。']
            assert len(created) == 1 and len(repo.list_task_attachments(data['project']['id'], created[0]['id'])) == 1
        finally:
            browser.close()


def test_file_selected_before_deferred_workspace_script_is_adopted(live_hub, repo):
    data = seed_workspace(repo, room_messages=1)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':1440, 'height':900})
        pending_scripts = []
        try:
            page.goto(live_hub + '/login')
            page.locator('[name="username"]').fill('owner')
            page.locator('[name="password"]').fill('owner-password')
            page.route('**/static/workspace-shell.js?*', lambda route: pending_scripts.append(route), times=1)
            page.locator('button[type="submit"]').click(no_wait_after=True)
            playwright.expect(page.locator('[data-ws-shell]')).to_be_visible()
            # HTML and native inputs can become visible before a deferred script
            # arrives, especially after login on a busy or slow connection.
            page.locator('[data-ws-input]').fill('页面刚出现时选中的文件仍应交给执行者。')
            page.locator('[data-ws-attachment-input]').set_input_files({
                'name':'启动期间选择.txt', 'mimeType':'text/plain', 'buffer':b'keep early file'})
            assert page.locator('[data-ws-attachment-input]').evaluate('(el)=>el.files.length') == 1
            assert pending_scripts
            pending_scripts.pop().continue_()
            playwright.expect(page.locator('[data-ws-attachment]')).to_have_count(1)
            assert page.locator('[data-ws-input]').input_value() == '页面刚出现时选中的文件仍应交给执行者。'
            with page.expect_navigation(wait_until='domcontentloaded'):
                page.locator('[data-ws-send]').click()
            created = [task for task in repo.list_tasks(data['project']['id'])
                       if task['description'] == '页面刚出现时选中的文件仍应交给执行者。']
            assert len(created) == 1
            assert repo.list_task_attachments(data['project']['id'], created[0]['id'])[0]['original_filename'] == '启动期间选择.txt'
        finally:
            for route in pending_scripts:
                route.continue_()
            browser.close()
