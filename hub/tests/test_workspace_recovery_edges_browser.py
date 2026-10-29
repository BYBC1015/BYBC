"""Headless checks for long conversations and delayed local dispatch storage."""
import pytest

from test_mobile_composer_browser import live_hub  # noqa: F401
from test_workspace_shell_browser import login_page
from workspace_fixture import seed_workspace

playwright = pytest.importorskip('playwright.sync_api')


def settle_layout(page):
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')


def open_room(page, live_hub, data):
    login_page(page, live_hub)
    url = f"{live_hub}/projects/workspace-team/conversations/{data['conversation_id']}"
    page.goto(url)
    playwright.expect(page.locator('[data-ws-shell]')).to_have_attribute('data-ws-ready', 'true')
    return url


def read_earlier_entry(page):
    entry = page.locator('[data-ws-live] [data-ws-entry]:not([data-ws-progress])').nth(5)
    entry_id = entry.get_attribute('data-ws-entry')
    entry.evaluate('''node => {
        const view=document.querySelector('[data-ws-scroll]');
        view.scrollTop += node.getBoundingClientRect().top-view.getBoundingClientRect().top+9;
    }''')
    settle_layout(page)
    return entry_id, entry.evaluate('node => node.getBoundingClientRect().top-document.querySelector("[data-ws-scroll]").getBoundingClientRect().top')


def test_sending_while_reading_history_reveals_the_new_user_message(live_hub, repo):
    data = seed_workspace(repo, room_messages=90)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width':1440, 'height':960})
            open_room(page, live_hub, data)
            read_earlier_entry(page)
            message = '阅读旧记录时发出的新要求，也应立即出现在视野里。'
            page.locator('[data-ws-input]').fill(message)
            with page.expect_navigation(wait_until='domcontentloaded'):
                page.locator('[data-ws-send]').click()
            playwright.expect(page.locator('[data-ws-shell]')).to_have_attribute('data-ws-ready', 'true')
            user_message = page.locator('.ws-message-user [data-ws-message-body]', has_text=message)
            playwright.expect(user_message).to_have_count(1)
            settle_layout(page)
            assert user_message.evaluate('''node => {
                const box=node.getBoundingClientRect(), view=document.querySelector('[data-ws-scroll]').getBoundingClientRect();
                return box.top>=view.top && box.bottom<=view.bottom;
            }'''), 'A successful send must reveal its persisted user message, not restore an old reading position'
            assert page.locator('[data-ws-input]').input_value() == ''
        finally:
            browser.close()


def test_offline_message_bursts_preserve_the_current_reading_anchor_and_draft(live_hub, repo):
    data = seed_workspace(repo, room_messages=90)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width':1440, 'height':960})
            page_errors = []
            page.on('pageerror', lambda error:page_errors.append(str(error)))
            room_url = open_room(page, live_hub, data)
            anchor, offset = read_earlier_entry(page)
            draft = '这是读旧记录时正在补写的内容，断线重连不能丢失。'
            page.locator('[data-ws-input]').fill(draft)
            page.evaluate('window.__readingAuditDocument = "same-page"')
            paused = {'value':True}

            def delay_refresh(route):
                if paused['value'] and route.request.resource_type == 'fetch':
                    route.abort()
                else:
                    route.continue_()

            page.route(room_url, delay_refresh)
            for batch in range(2):
                paused['value'] = True
                final = None
                for index in range(45):
                    final = repo.create_entry(data['project']['id'], source_label='隔离回归执行者', kind='message',
                        body_markdown=f'离线期间新增进展 {batch}-{index}', task_id=data['tasks'][2]['id'],
                        author_device_id=data['connection']['id'])
                paused['value'] = False
                page.evaluate('window.dispatchEvent(new Event("online"))')
                playwright.expect(page.locator(f'[data-ws-entry="{final["id"]}"]')).to_have_count(1, timeout=12000)
                anchor_node = page.locator(f'[data-ws-entry="{anchor}"]')
                playwright.expect(anchor_node).to_have_count(1)
                settle_layout(page)
                actual_offset = anchor_node.evaluate('node => node.getBoundingClientRect().top-document.querySelector("[data-ws-scroll]").getBoundingClientRect().top')
                assert abs(actual_offset-offset) <= 2
                assert page.locator('[data-ws-input]').input_value() == draft
                assert page.evaluate('window.__readingAuditDocument') == 'same-page'
                assert page.locator('[data-ws-entry]:not([data-ws-progress])').count() <= 80
                playwright.expect(page.locator('[data-ws-new-messages]')).to_be_visible()
                playwright.expect(page.locator('[data-ws-history-gap]')).to_have_count(1)
                with page.expect_response(lambda response: response.url==room_url and response.request.resource_type=='fetch'):
                    page.evaluate('window.dispatchEvent(new Event("online"))')
                playwright.expect(page.locator('[data-ws-history-gap]')).to_have_count(1)
            assert not page_errors
        finally:
            browser.close()


def test_slow_local_enqueue_locks_accepted_input_before_it_can_be_overwritten(live_hub, repo):
    data = seed_workspace(repo, room_messages=2)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width':1440, 'height':960})
            page.add_init_script('''(() => {
                let store;
                Object.defineProperty(window, 'CodexWorkspaceStore', {
                    get:()=>store,
                    set:value=>{store={...value, enqueue:async(...args)=>{
                        if (window.__delayEnqueue) {
                            window.__enqueueCalls=(window.__enqueueCalls||0)+1;
                            await new Promise(resolve=>window.__releaseEnqueue=resolve);
                        }
                        return value.enqueue(...args);
                    }}}});
            })()''')
            open_room(page, live_hub, data)
            page.evaluate('window.__delayEnqueue=true')
            message = '慢速本机存储也只能发送我点击时确认的这一条。'
            page.locator('[data-ws-input]').fill(message)
            page.locator('[data-ws-send]').click()
            page.wait_for_function('window.__enqueueCalls === 1')
            assert not page.locator('[data-ws-input]').is_editable(), 'The accepted payload is captured but the editor still allows edits that adoptJob will overwrite'
            assert page.locator('[data-ws-send]').is_disabled()
            with page.expect_navigation(wait_until='domcontentloaded'):
                page.evaluate('window.__releaseEnqueue()')
            playwright.expect(page.locator('.ws-message-user [data-ws-message-body]', has_text=message)).to_have_count(1)
            assert len([task for task in repo.list_tasks(data['project']['id']) if task['description']==message]) == 1
        finally:
            browser.close()


@pytest.mark.parametrize('clear_text', [False, True], ids=['new-text', 'explicit-clear'])
def test_typing_during_slow_draft_restore_keeps_the_new_text(live_hub, repo, clear_text):
    data = seed_workspace(repo, room_messages=2)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width':1440, 'height':960})
            page.add_init_script('''(() => {
                let store;
                Object.defineProperty(window, 'CodexWorkspaceStore', {
                    get:()=>store,
                    set:value=>{store={...value, read:async(...args)=>{
                        if (sessionStorage.getItem('auditDelayRestore')==='yes') {
                            sessionStorage.removeItem('auditDelayRestore');
                            window.__restoreWaiting=true;
                            await new Promise(resolve=>window.__releaseRestore=resolve);
                        }
                        return value.read(...args);
                    }}}});
            })()''')
            open_room(page, live_hub, data)
            old = '上一次还没有发送的旧草稿。'
            new = '页面恢复过程中已经重新输入的新草稿，不应被旧内容覆盖。'
            page.locator('[data-ws-input]').fill(old)
            playwright.expect(page.locator('[data-ws-draft-status]')).to_contain_text('已保存在本机')
            page.evaluate('sessionStorage.setItem("auditDelayRestore","yes")')
            page.reload(wait_until='domcontentloaded')
            page.wait_for_function('window.__restoreWaiting === true')
            page.locator('[data-ws-input]').fill(new)
            if clear_text:
                page.locator('[data-ws-input]').fill('')
                new = ''
            page.evaluate('window.__releaseRestore()')
            playwright.expect(page.locator('[data-ws-shell]')).to_have_attribute('data-ws-ready', 'true')
            assert page.locator('[data-ws-input]').input_value() == new
            playwright.expect(page.locator('[data-ws-draft-status]')).to_contain_text('已保存在本机')
            page.reload()
            playwright.expect(page.locator('[data-ws-shell]')).to_have_attribute('data-ws-ready', 'true')
            assert page.locator('[data-ws-input]').input_value() == new
        finally:
            browser.close()
