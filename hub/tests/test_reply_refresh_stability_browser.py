"""Real browser checks for copying live replies and honest recovery states."""
import time

import pytest

from test_mobile_composer_browser import live_hub  # noqa: F401
from test_workspace_shell_browser import login_page
from test_desktop_progress_browser import running_task, publish, open_room
from workspace_fixture import seed_workspace

playwright = pytest.importorskip('playwright.sync_api')


def test_selected_reply_is_retained_but_execution_status_keeps_updating(live_hub, repo):
    data = seed_workspace(repo, room_messages=2)
    task, run, headers = running_task(repo, data)
    publish(live_hub, task, run, headers, 1, 'assistant', '这段文字正在复制，刷新不能打断。')
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width':1440, 'height':960})
            login_page(page, live_hub)
            open_room(page, live_hub, data['conversation_id'])
            page.locator('[data-ws-input]').fill('同时保留我正在写的草稿')
            card = page.locator(f'[data-ws-progress="{task["id"]}"]')
            card.locator('[data-ws-progress-details]').evaluate('node => node.open = true')
            body = card.locator('[data-ws-progress-body]')
            body.evaluate('''node => {
                window.__retainedReplyBody=node;
                window.__retainedReplyParagraph=node.firstElementChild;
                const range=document.createRange(); range.selectNodeContents(node);
                const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);
            }''')
            selected = page.evaluate('window.getSelection().toString()')
            publish(live_hub, task, run, headers, 2, 'assistant', '这段文字正在复制，刷新不能打断。\n\n新增公开回复。')
            publish(live_hub, task, run, headers, 3, 'status', '正在检查第二个文件')
            playwright.expect(card.locator('[data-ws-progress-status-text]')).to_have_text('正在检查第二个文件', timeout=8000)
            assert page.evaluate('window.getSelection().toString()') == selected
            assert body.evaluate('node => node === window.__retainedReplyBody && node.firstElementChild === window.__retainedReplyParagraph')
            assert '新增公开回复' not in body.inner_text()
            assert card.locator('[data-ws-progress-details]').evaluate('node => node.open')
            with repo.connection:
                repo.connection.execute('UPDATE hub_task_executions SET lease_until=? WHERE task_id=?', (time.time()-1, task['id']))
            page.evaluate('window.dispatchEvent(new Event("online"))')
            playwright.expect(card.locator('[data-ws-progress-label-text]')).to_have_text('执行状态待核对', timeout=8000)
            assert page.evaluate('window.getSelection().toString()') == selected
            page.evaluate('window.getSelection().removeAllRanges()')
            playwright.expect(body).to_contain_text('新增公开回复', timeout=8000)
            assert body.evaluate('node => node === window.__retainedReplyBody')
            assert card.locator('[data-ws-progress-details]').evaluate('node => node.open')
            assert page.locator('[data-ws-input]').input_value() == '同时保留我正在写的草稿'
        finally:
            browser.close()


def test_recovery_requires_fresh_workspace_not_just_online_event_or_304(live_hub, repo):
    data = seed_workspace(repo, room_messages=2)
    task, run, headers = running_task(repo, data)
    publish(live_hub, task, run, headers, 1, 'assistant', '已经收到的回复在断线后保留。')
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width':1440, 'height':960})
            login_page(page, live_hub)
            open_room(page, live_hub, data['conversation_id'])
            card = page.locator(f'[data-ws-progress="{task["id"]}"]')
            shell = page.locator('[data-ws-shell]')
            url = page.url
            attempts = []
            mode = {'value':'stale'}

            def intercept(route):
                if route.request.resource_type == 'fetch' and mode['value'] == 'stale':
                    attempts.append(route.request.headers)
                    route.fulfill(status=304, headers={'ETag':'"stale"'})
                else:
                    route.continue_()

            page.route(url, intercept)
            page.evaluate('window.dispatchEvent(new Event("offline")); window.dispatchEvent(new Event("online"))')
            playwright.expect(shell).to_have_attribute('data-ws-connection', 'offline', timeout=8000)
            assert attempts and attempts[-1].get('x-hub-online-probe') == '1'
            playwright.expect(card.locator('[data-reply-connection-note]')).to_be_visible()
            playwright.expect(card.locator('[data-ws-progress-body]')).to_have_text('已经收到的回复在断线后保留。')
            mode['value'] = 'fresh'
            page.evaluate('window.dispatchEvent(new Event("online"))')
            playwright.expect(shell).to_have_attribute('data-ws-connection', 'online', timeout=8000)
            playwright.expect(card.locator('[data-reply-connection-note]')).to_be_hidden()
        finally:
            browser.close()


def test_first_public_text_on_existing_placeholder_marks_unread_without_moving_reader(live_hub, repo):
    data = seed_workspace(repo, room_messages=90)
    task, run, headers = running_task(repo, data)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width':1440, 'height':960})
            login_page(page, live_hub)
            open_room(page, live_hub, data['conversation_id'])
            card = page.locator(f'[data-ws-progress="{task["id"]}"]')
            playwright.expect(card).to_have_count(1)
            card.evaluate('node => window.__initialReplyCard = node')
            view = page.locator('[data-ws-scroll]')
            view.evaluate('node => {node.scrollTop=300;node.dispatchEvent(new Event("scroll"));}')
            top = view.evaluate('node => node.scrollTop')
            playwright.expect(page.locator('[data-ws-new-messages]')).to_be_hidden()
            publish(live_hub, task, run, headers, 1, 'assistant', '第一份进展已到达。')
            playwright.expect(card.locator('[data-ws-progress-body]')).to_have_text('第一份进展已到达。', timeout=8000)
            playwright.expect(page.locator('[data-ws-new-messages]')).to_be_visible()
            assert card.evaluate('node => node === window.__initialReplyCard')
            assert abs(view.evaluate('node => node.scrollTop')-top) < 4
        finally:
            browser.close()
