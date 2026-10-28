"""Browser regressions against a local Hub, without any live AI workers."""
import uuid
from pathlib import Path

import httpx
import pytest

from codex_hub.execution_leases import ExecutionStore
from test_mobile_composer_browser import live_hub  # noqa: F401
from test_workspace_shell_browser import login_page
from workspace_fixture import seed_workspace

playwright = pytest.importorskip('playwright.sync_api')
OUTPUT = Path(__file__).resolve().parents[2] / 'output/hub-desktop-20261029/browser'


def running_task(repo, data, *, new_room=False):
    task = repo.create_task(data['project']['id'], '实时进度浏览器测试', assigned_agent_id=data['controller']['id'])
    route = {'action': 'new', 'title': '另一场独立对话', 'reason': '验证隔离'} if new_room else {
        'action': 'reuse', 'conversation_id': data['conversation_id'], 'reason': '当前对话继续处理'}
    data['store'].route(task['id'], route, 'codex_cli')
    repo.connection.execute("UPDATE tasks SET execution_route='chat' WHERE id=?", (task['id'],))
    repo.connection.commit()
    store = ExecutionStore(repo)
    run = store.acquire(task['id'], data['connection']['id'], 'browser-progress-' + uuid.uuid4().hex)['execution']
    run = store.start(task['id'], data['connection']['id'], run['generation'], run['run_key'])['execution']
    token = repo.create_api_token(device_id=data['connection']['id'], name='isolated-browser-progress')
    return repo.get_task(task['id']), run, {'Authorization': f'Bearer {token.raw}'}


def publish(base, task, run, headers, sequence, kind, text):
    response = httpx.post(f"{base}/api/projects/workspace-team/tasks/{task['id']}/execution/progress",
        headers=headers, json={'generation': run['generation'], 'run_key': run['run_key'],
            'sequence': sequence, 'kind': kind, 'text': text}, timeout=5)
    assert response.status_code == 200, response.text


def open_room(page, base, room):
    page.goto(f'{base}/projects/workspace-team/conversations/{room}')
    page.wait_for_function("document.querySelector('[data-ws-shell]').dataset.wsReady === 'true'")


@pytest.mark.parametrize('width', [1440, 390])
def test_live_public_progress_replaced_by_one_final_reply_preserves_draft_and_reading(live_hub, repo, width):
    data = seed_workspace(repo, room_messages=70)
    task, run, headers = running_task(repo, data)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': width, 'height': 960})
        errors, streams = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: streams.append(request.url) if request.resource_type == 'eventsource' else None)
        try:
            login_page(page, live_hub)
            open_room(page, live_hub, data['conversation_id'])
            page.evaluate('window.__sameProgressDocument = true')
            draft = '我还在补充下一项修改，不应被进度刷新打断。'
            page.locator('[data-ws-input]').fill(draft)
            scroll = page.locator('[data-ws-scroll]')
            scroll.evaluate('el => { el.scrollTop = Math.min(500, (el.scrollHeight-el.clientHeight)/3); el.dispatchEvent(new Event("scroll")); }')
            original_top = scroll.evaluate('el => el.scrollTop')
            assert original_top > 0
            progress = page.locator(f'[data-ws-progress="{task["id"]}"]')
            publish(live_hub, task, run, headers, 1, 'status', '正在核对素材')
            playwright.expect(progress.locator('.ws-progress-status')).to_have_text('最近报告：正在核对素材', timeout=8000)
            publish(live_hub, task, run, headers, 2, 'assistant', '收到，我正在逐项处理。')
            playwright.expect(progress.locator('[data-ws-message-body]')).to_have_text('收到，我正在逐项处理。', timeout=8000)
            publish(live_hub, task, run, headers, 3, 'assistant', '收到，我正在逐项处理。\n\n第一项已经完成，你可以继续补充。')
            playwright.expect(progress).to_contain_text('第一项已经完成，你可以继续补充', timeout=8000)
            playwright.expect(progress).to_have_count(1)
            assert page.locator('[data-ws-input]').input_value() == draft
            assert abs(scroll.evaluate('el => el.scrollTop') - original_top) < 4
            assert page.evaluate('window.__sameProgressDocument')
            playwright.expect(page.locator('[data-ws-new-messages]')).to_be_visible()
            assert any(url.endswith('/projects/workspace-team/events') for url in streams)

            final = '正式交付：两项修改已经完成，验收记录已保存。'
            response = httpx.post(f"{live_hub}/api/projects/workspace-team/tasks/{task['id']}/execution/complete",
                headers=headers, json={'generation': run['generation'], 'run_key': run['run_key'],
                    'status': 'review', 'summary': '两项修改已完成', 'body': final,
                    'reflection': {'summary': '已验证', 'worked': '逐项验证', 'failed': '', 'next_time': '保留回归'}}, timeout=5)
            assert response.status_code == 200, response.text
            result = page.locator('[data-ws-entry]').filter(has_text=final)
            playwright.expect(result).to_have_count(1, timeout=8000)
            playwright.expect(progress).to_have_count(0)
            assert page.locator('[data-ws-live]').inner_text().count(final) == 1
            assert '第一项已经完成，你可以继续补充' not in page.locator('[data-ws-live]').inner_text()
            assert page.locator('[data-ws-input]').input_value() == draft
            assert abs(scroll.evaluate('el => el.scrollTop') - original_top) < 4
            assert page.evaluate('window.__sameProgressDocument')
            page.locator('[data-ws-new-messages]').click()
            playwright.expect(result).to_be_in_viewport()
            OUTPUT.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(OUTPUT / f'progress-final-{width}.png'))
            assert not errors
        finally:
            browser.close()


def test_progress_never_crosses_conversations_during_live_updates(live_hub, repo):
    data = seed_workspace(repo, room_messages=2)
    first_task, first_run, first_headers = running_task(repo, data)
    second_task, second_run, second_headers = running_task(repo, data, new_room=True)
    publish(live_hub, first_task, first_run, first_headers, 1, 'assistant', '第一场对话的公开回复')
    publish(live_hub, second_task, second_run, second_headers, 1, 'assistant', '第二场对话的独立回复')
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 960})
        try:
            login_page(page, live_hub)
            open_room(page, live_hub, data['conversation_id'])
            playwright.expect(page.locator(f'[data-ws-progress="{first_task["id"]}"]')).to_contain_text('第一场对话')
            assert page.locator(f'[data-ws-progress="{second_task["id"]}"]').count() == 0
            publish(live_hub, second_task, second_run, second_headers, 2, 'assistant', '第二场对话的独立回复，继续更新')
            publish(live_hub, first_task, first_run, first_headers, 2, 'assistant', '第一场对话的公开回复，当前可见')
            playwright.expect(page.locator('[data-ws-live]')).to_contain_text('当前可见', timeout=8000)
            assert '第二场对话的独立回复' not in page.locator('[data-ws-live]').inner_text()
            open_room(page, live_hub, second_task['conversation_id'])
            playwright.expect(page.locator(f'[data-ws-progress="{second_task["id"]}"]')).to_contain_text('继续更新')
            assert page.locator(f'[data-ws-progress="{first_task["id"]}"]').count() == 0
            assert '第一场对话的公开回复' not in page.locator('[data-ws-live]').inner_text()
        finally:
            browser.close()


def test_progress_age_updates_without_new_packets_and_preserves_reading(live_hub, repo, monkeypatch):
    import time
    import codex_hub.execution_progress as progress_module
    monkeypatch.setattr(progress_module, 'ACTIVITY_CLOCK_SECONDS', 1)
    data = seed_workspace(repo, room_messages=70)
    task, run, headers = running_task(repo, data)
    publish(live_hub, task, run, headers, 1, 'assistant', '已收到，正在处理原有工作。')
    publish(live_hub, task, run, headers, 2, 'status', '正在执行工具')
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':1440,'height':960})
        try:
            login_page(page, live_hub)
            open_room(page, live_hub, data['conversation_id'])
            page.locator('[data-ws-input]').fill('计时更新期间继续编辑的草稿')
            scroll = page.locator('[data-ws-scroll]')
            scroll.evaluate('el => { el.scrollTop=300; el.dispatchEvent(new Event("scroll")); }')
            original_top = scroll.evaluate('el => el.scrollTop')
            indicator = page.locator(f'[data-ws-progress="{task["id"]}"]')
            version = indicator.get_attribute('data-ws-entry-version')
            page.wait_for_function('([id, version]) => document.querySelector(`[data-ws-progress="${id}"]`)?.dataset.wsEntryVersion !== version',
                arg=[task['id'],version],timeout=8000)
            playwright.expect(indicator.locator('[data-ws-progress-timing]')).to_contain_text('距上次进度约')
            playwright.expect(indicator.locator('[data-ws-message-body]')).to_have_text('已收到，正在处理原有工作。')
            assert repo.connection.execute('SELECT sequence FROM hub_execution_progress WHERE task_id=?',(task['id'],)).fetchone()[0] == 2
            assert page.locator('[data-ws-input]').input_value() == '计时更新期间继续编辑的草稿'
            assert abs(scroll.evaluate('el => el.scrollTop')-original_top) < 4
            repo.connection.execute('UPDATE hub_task_executions SET lease_until=? WHERE task_id=?',(time.time()-1,task['id']))
            repo.connection.commit()
            playwright.expect(indicator).to_contain_text('执行状态待核对',timeout=8000)
            playwright.expect(indicator.locator('[data-ws-progress-timing]')).to_contain_text('任务联系已过期')
            assert page.locator('[data-ws-input]').input_value() == '计时更新期间继续编辑的草稿'
            assert abs(scroll.evaluate('el => el.scrollTop')-original_top) < 4
        finally:
            browser.close()


def test_native_project_button_uses_stable_id_and_preserves_current_draft(live_hub, repo):
    data = seed_workspace(repo, room_messages=2)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 960})
        page.add_init_script('''
            window.__openedProjects = [];
            window.pywebview = {api: {
                list_projects: async () => [
                    {id:'local-project-opaque-id',name:'当前制作工程',available:true},
                    {id:'offline-project',name:'暂不可用的工程',available:false}],
                open_project: async id => {window.__openedProjects.push(id);return {opened:true};}
            }};
        ''')
        try:
            login_page(page, live_hub)
            open_room(page, live_hub, data['conversation_id'])
            page.locator('[data-ws-input]').fill('打开本机目录时保留正在编辑的文字')
            button = page.locator('[data-ws-local-projects]')
            playwright.expect(button).to_have_count(1)
            page.evaluate('window.dispatchEvent(new Event("pywebviewready"))')
            playwright.expect(button).to_have_count(1)
            button.click()
            dialog = page.locator('dialog.ws-local-dialog')
            playwright.expect(dialog).to_be_visible()
            playwright.expect(dialog.get_by_role('button', name='暂不可用的工程')).to_be_disabled()
            dialog.get_by_role('button', name='当前制作工程', exact=True).click()
            playwright.expect(dialog.locator('[data-local-state]')).to_have_text('已打开工程文件夹。')
            assert page.evaluate('window.__openedProjects') == ['local-project-opaque-id']
            dialog.get_by_role('button', name='关闭', exact=True).click()
            playwright.expect(dialog).not_to_be_visible()
            playwright.expect(button).to_be_focused()
            assert page.locator('[data-ws-input]').input_value() == '打开本机目录时保留正在编辑的文字'
        finally:
            browser.close()


EVENT_SOURCE_STUB = '''
    window.__eventSources = [];
    window.__workspaceFetches = [];
    const nativeFetch = window.fetch.bind(window);
    window.fetch = (...args) => {
        const url = String(args[0]?.url || args[0]);
        if (url.includes('/conversations/')) window.__workspaceFetches.push({url,at:performance.now()});
        return nativeFetch(...args);
    };
    window.EventSource = class extends EventTarget {
        constructor(url) {
            super();this.url=url;this.closed=false;this.readyState=0;
            window.__eventSources.push(this);
            setTimeout(() => this.emit('open'),0);
        }
        emit(type,data={}) {
            if(this.closed)return;
            if(type==='open')this.readyState=1;
            if(type==='error')this.readyState=0;
            this.dispatchEvent(new MessageEvent(type,{data:JSON.stringify(data)}));
        }
        close() {this.closed=true;this.readyState=2;}
    };
'''


def test_event_stream_failure_falls_back_to_polling_recovers_and_session_expiry_stops_reconnect(live_hub, repo):
    data = seed_workspace(repo, room_messages=2)
    task, run, headers = running_task(repo, data)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 960})
        page.add_init_script(EVENT_SOURCE_STUB)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            login_page(page, live_hub)
            open_room(page, live_hub, data['conversation_id'])
            page.wait_for_function('window.__eventSources.length === 1 && window.__eventSources[0].readyState === 1')
            # Let the initial scheduled fetch finish; healthy SSE subsequently
            # uses the slower 10-second fallback, so this exercises an event.
            page.wait_for_function('window.__workspaceFetches.length >= 1')
            initial = page.evaluate('window.__workspaceFetches.length')
            page.evaluate("window.__eventSources[0].emit('error')")
            publish(live_hub, task, run, headers, 1, 'assistant', '推送断开后仍收到回复')
            progress = page.locator(f'[data-ws-progress="{task["id"]}"]')
            playwright.expect(progress).to_contain_text('推送断开后仍收到回复', timeout=5000)
            page.wait_for_function('count => window.__workspaceFetches.length >= count + 2', arg=initial, timeout=5000)

            # The browser EventSource reconnects the same object automatically.
            page.evaluate("window.__eventSources[0].emit('open')")
            publish(live_hub, task, run, headers, 2, 'assistant', '推送断开后仍收到回复，实时连接已恢复')
            page.evaluate("window.__eventSources[0].emit('workspace', {version:'changed-after-reconnect'})")
            playwright.expect(progress).to_contain_text('实时连接已恢复', timeout=4000)
            assert page.evaluate('window.__eventSources.length') == 1

            page.locator('[data-ws-input]').fill('连接恢复时保留我的草稿')
            session = next(cookie['value'] for cookie in page.context.cookies() if cookie['name'] == 'codex_hub_session')
            repo.delete_session(session)
            page.evaluate("window.__eventSources[0].emit('session-expired')")
            page.wait_for_function('window.__eventSources[0].closed === true')
            page.evaluate('''() => {
                window.dispatchEvent(new Event('offline'));
                window.dispatchEvent(new Event('online'));
                window.dispatchEvent(new Event('pageshow'));
                document.dispatchEvent(new Event('visibilitychange'));
            }''')
            # Force an asynchronous browser turn after lifecycle events without
            # waiting for the server's normal 55-second SSE rollover.
            playwright.expect(page.locator('[data-ws-sync]')).to_contain_text('登录已过期')
            playwright.expect(page.locator('[data-ws-sync] a')).to_have_attribute('href', '/login')
            assert page.evaluate('window.__eventSources.length') == 1
            assert page.locator('[data-ws-input]').input_value() == '连接恢复时保留我的草稿'
            assert not errors
        finally:
            browser.close()
