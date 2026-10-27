from pathlib import Path
import pytest
from test_mobile_composer_browser import live_hub
from test_workspace_shell_browser import login_page
from test_execution_leases import lease_task, start, RESULT
from codex_hub.execution_leases import ExecutionStore
from codex_hub.handoffs import ask_handoff

playwright = pytest.importorskip('playwright.sync_api')


def test_real_handoff_updates_in_current_chat_without_refresh(live_hub, repo, lease_task):
    d = lease_task
    store = ExecutionStore(repo)
    execution = start(store, d)
    task_id = d['task']['id']
    room = repo.get_task(task_id)['conversation_id']
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 960})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            login_page(page, live_hub)
            page.goto(f"{live_hub}/projects/lease-testing/conversations/{room}")
            page.locator('[data-ws-input]').fill('我正在补充说明，不要丢失输入')
            page.evaluate('window.handoffDocumentMarker = "original-document"')
            handoff = ask_handoff(repo, repo.get_task(task_id), d['device']['id'], {
                'execution': execution, 'question': '横版还是竖版？原来的标题需要保留吗？',
                'request_key': 'browser-real-handoff-001'})
            question = page.locator(f'[data-ws-entry="{handoff["question_entry_id"]}"]')
            playwright.expect(question).to_contain_text('横版还是竖版', timeout=12000)
            store.complete(task_id, d['device']['id'], execution['generation'], execution['run_key'], **RESULT)
            reply_task = repo.get_task(handoff['controller_task_id'])
            reply_execution = start(store, {**d, 'task': reply_task}, 'browser-controller-answer')
            store.complete(reply_task['id'], d['device']['id'], reply_execution['generation'], reply_execution['run_key'],
                           **{**RESULT, 'body': '保留原标题，采用 16:9 横版；只调整画面构图。'})
            answer = page.locator('[data-ws-entry]').filter(has_text='保留原标题，采用 16:9 横版')
            playwright.expect(answer).to_have_count(1, timeout=12000)
            playwright.expect(page.locator('[data-ws-shell]')).not_to_contain_text('等待总控交接答复，其他独立工作继续', timeout=12000)
            assert page.evaluate('window.handoffDocumentMarker') == 'original-document'
            assert page.locator('[data-ws-input]').input_value() == '我正在补充说明，不要丢失输入'
            assert not errors
            output = Path(__file__).resolve().parents[2] / 'output/hub-execution-audit-20261028/browser'
            output.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output / 'visible-handoff.png'))
        finally:
            browser.close()
