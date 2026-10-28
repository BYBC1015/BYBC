"""Process restart and uncertain-result regressions against an isolated real Hub."""
import hashlib
from pathlib import Path
import pytest
from test_mobile_composer_browser import live_hub
from test_workspace_shell_browser import login_page, assert_composer_visible
from workspace_fixture import seed_workspace
playwright = pytest.importorskip('playwright.sync_api')
DATA = '桌面重启后必须仍然是这份原始资料。'.encode()
OUTPUT = Path(__file__).resolve().parents[2] / 'output/hub-desktop-local-20261029'


def ready(page):
    playwright.expect(page.locator('[data-ws-shell]')).to_have_attribute('data-ws-ready','true')


def test_explicit_offline_queue_survives_browser_process_restart_and_sends_once(live_hub,repo,tmp_path):
    data=seed_workspace(repo,room_messages=1)
    with playwright.sync_playwright() as runtime:
        profile=tmp_path/'browser-profile'
        context=runtime.chromium.launch_persistent_context(str(profile),headless=True)
        page=context.pages[0]
        login_page(page,live_hub)
        description='明确点击发送后，即使重启程序也能可靠续发。'
        page.locator('[data-ws-input]').fill(description)
        page.locator('[data-ws-attachment-input]').set_input_files({'name':'重启资料.txt','mimeType':'text/plain','buffer':DATA})
        playwright.expect(page.locator('[data-ws-draft-status]')).to_contain_text('保存在本机')
        context.set_offline(True)
        page.locator('[data-ws-send]').click()
        playwright.expect(page.locator('[data-ws-queue-cancel]')).to_be_visible()
        url=page.url
        context.close()
        context=runtime.chromium.launch_persistent_context(str(profile),headless=True)
        page=context.pages[0]
        posts=[]
        page.on('request',lambda request:posts.append(request.url) if request.method=='POST' and request.url.endswith('/workspace-dispatch') else None)
        try:
            page.goto(url)
            if page.locator('[name="username"]').count():
                page.locator('[name="username"]').fill('owner')
                page.locator('[name="password"]').fill('owner-password')
                page.locator('button[type="submit"]').click()
            page.wait_for_url('**/conversations/*')
            ready(page)
            created=[task for task in repo.list_tasks(data['project']['id']) if task['description']==description]
            assert len(created)==1 and len(posts)==1
            files=repo.list_task_attachments(data['project']['id'],created[0]['id'])
            assert len(files)==1 and files[0]['sha256']==hashlib.sha256(DATA).hexdigest()
            playwright.expect(page.locator('[data-ws-input]')).to_have_value('')
        finally:context.close()


def test_lost_response_restart_checks_receipt_without_posting_again(live_hub,repo,tmp_path):
    data=seed_workspace(repo,room_messages=1)
    with playwright.sync_playwright() as runtime:
        profile=tmp_path/'uncertain-profile'
        context=runtime.chromium.launch_persistent_context(str(profile),headless=True)
        page=context.pages[0]
        login_page(page,live_hub)
        description='服务器收到但响应丢失，重启后只查询收据。'
        page.locator('[data-ws-input]').fill(description)
        page.locator('[data-ws-attachment-input]').set_input_files({'name':'响应丢失资料.txt','mimeType':'text/plain','buffer':DATA})
        page.route('**/workspace-dispatch/*',lambda route:route.abort())
        def lose_response(route):
            response=route.fetch()
            route.fulfill(response=response)
        page.route('**/workspace-dispatch',lose_response,times=1)
        page.locator('[data-ws-send]').click()
        playwright.expect(page.locator('[data-ws-dispatch-error]')).to_contain_text('未确认任务已安排')
        playwright.expect(page.locator('[data-ws-pending]')).to_be_visible()
        url=page.url
        context.close()
        context=runtime.chromium.launch_persistent_context(str(profile),headless=True)
        page=context.pages[0];posts=[]
        page.on('request',lambda request:posts.append(request.url) if request.method=='POST' and request.url.endswith('/workspace-dispatch') else None)
        try:
            page.goto(url)
            if page.locator('[name="username"]').count():
                page.locator('[name="username"]').fill('owner')
                page.locator('[name="password"]').fill('owner-password')
                page.locator('button[type="submit"]').click()
            page.wait_for_url('**/conversations/*')
            ready(page)
            created=[task for task in repo.list_tasks(data['project']['id']) if task['description']==description]
            assert len(created)==1 and not posts
            files=repo.list_task_attachments(data['project']['id'],created[0]['id'])
            assert len(files)==1 and files[0]['sha256']==hashlib.sha256(DATA).hexdigest()
        finally:context.close()


@pytest.mark.parametrize('width',[1440,390])
def test_pending_queue_is_visible_in_other_conversation_without_losing_new_draft(live_hub,repo,width):
    data=seed_workspace(repo,room_messages=1)
    with playwright.sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        context=browser.new_context(viewport={'width':width,'height':900})
        page=context.new_page()
        try:
            login_page(page,live_hub)
            page.locator('[data-ws-input]').fill('待发送：请检查这份本机资料。')
            page.locator('[data-ws-attachment-input]').set_input_files({'name':'队列资料.txt','mimeType':'text/plain','buffer':DATA})
            context.set_offline(True)
            page.locator('[data-ws-send]').click()
            playwright.expect(page.locator('[data-ws-queue-cancel]')).to_be_visible()
            OUTPUT.mkdir(parents=True,exist_ok=True)
            assert_composer_visible(page,900)
            page.screenshot(path=str(OUTPUT/f'queue-{width}.png'))
            # Preserve the queued job while leaving for a different conversation.
            # Block online probes only; ordinary navigation remains a real GET.
            def fail_probes(route):
                if route.request.resource_type in {'fetch','xhr'}:route.abort()
                else:route.continue_()
            page.route('**/projects/workspace-team',fail_probes)
            context.set_offline(False)
            page.goto(f"{live_hub}/projects/workspace-team/conversations/{data['conversation_id']}")
            ready(page)
            playwright.expect(page.locator('[data-ws-other-queues]')).to_contain_text('1 条待发送')
            page.locator('[data-ws-input]').fill('另一场对话的新草稿仍要保留。')
            assert_composer_visible(page,900)
            page.screenshot(path=str(OUTPUT/f'other-queue-{width}.png'))
            assert not [task for task in repo.list_tasks(data['project']['id']) if task['description']=='待发送：请检查这份本机资料。']
            page.unroute('**/projects/workspace-team')
            page.locator('[data-ws-other-queues] a').click()
            page.wait_for_url('**/conversations/*')
            created=[task for task in repo.list_tasks(data['project']['id']) if task['description']=='待发送：请检查这份本机资料。']
            assert len(created)==1
            page.goto(f"{live_hub}/projects/workspace-team/conversations/{data['conversation_id']}")
            ready(page)
            playwright.expect(page.locator('[data-ws-input]')).to_have_value('另一场对话的新草稿仍要保留。')
        finally:browser.close()


def test_unavailable_local_storage_never_claims_offline_queue_is_saved(live_hub,repo):
    data=seed_workspace(repo,room_messages=1)
    with playwright.sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        context=browser.new_context()
        page=context.new_page()
        page.add_init_script("indexedDB.open = () => { throw new DOMException('storage unavailable', 'SecurityError'); };")
        try:
            login_page(page,live_hub)
            description='本机存储失败时不要误报已离线排队。'
            page.locator('[data-ws-input]').fill(description)
            page.locator('[data-ws-attachment-input]').set_input_files({'name':'仅当前窗口.txt','mimeType':'text/plain','buffer':DATA})
            context.set_offline(True)
            page.locator('[data-ws-send]').click()
            playwright.expect(page.locator('[data-ws-dispatch-error]')).to_contain_text('无法离线排队')
            playwright.expect(page.locator('[data-ws-pending]')).to_be_hidden()
            playwright.expect(page.locator('[data-ws-attachment]')).to_have_count(1)
            assert not [task for task in repo.list_tasks(data['project']['id']) if task['description']==description]
            context.set_offline(False)
            playwright.expect(page.locator('[data-ws-sync]')).to_have_text('已同步')
            page.locator('[data-ws-send]').click()
            page.wait_for_url('**/conversations/*')
            created=[task for task in repo.list_tasks(data['project']['id']) if task['description']==description]
            assert len(created)==1
            files=repo.list_task_attachments(data['project']['id'],created[0]['id'])
            assert len(files)==1 and files[0]['sha256']==hashlib.sha256(DATA).hexdigest()
        finally:browser.close()


def test_hanging_receipt_times_out_and_preserves_uncertain_queue(live_hub,repo):
    data=seed_workspace(repo,room_messages=1)
    with playwright.sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        context=browser.new_context()
        page=context.new_page(); held=[]
        try:
            login_page(page,live_hub)
            description='收到请求后收据连接卡住，仍必须解除上传状态并保留原队列。'
            page.locator('[data-ws-input]').fill(description)
            page.locator('[data-ws-attachment-input]').set_input_files({'name':'收据超时资料.txt','mimeType':'text/plain','buffer':DATA})
            page.route('**/workspace-dispatch/*',lambda route:held.append(route))
            def lose_response(route):
                response=route.fetch()
                route.fulfill(response=response)
            page.route('**/workspace-dispatch',lose_response,times=1)
            page.locator('[data-ws-send]').click()
            # This deliberately waits for the real 12s product timeout, without
            # shortening its timer or widening any ordinary dispatch assertions.
            playwright.expect(page.locator('[data-ws-pending-message]')).to_contain_text('暂时无法确认',timeout=18000)
            context.set_offline(True)
            playwright.expect(page.locator('[data-ws-send]')).to_have_attribute('aria-busy','false')
            playwright.expect(page.locator('[data-ws-attachment]')).to_have_count(1)
            playwright.expect(page.locator('[data-ws-queue-cancel]')).to_be_hidden()
            assert held
            assert len([task for task in repo.list_tasks(data['project']['id']) if task['description']==description])==1
            state=page.evaluate("""async () => {
                const s=document.querySelector('[data-ws-shell]').dataset;
                const {job}=await CodexWorkspaceStore.read(`${s.user}.${s.project}.${s.conversation}`);
                return {attempted:job.attempted,state:job.state,text:await job.payload.files[0].file.text()};
            }""")
            assert state=={'attempted':True,'state':'uncertain','text':DATA.decode()}
        finally:
            for route in held:
                try:route.abort()
                except Exception:pass
            browser.close()
