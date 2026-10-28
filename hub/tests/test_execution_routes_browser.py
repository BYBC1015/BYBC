"""Real DOM regression for work surfaces without hiding chat transcripts."""
from pathlib import Path

import pytest

from test_mobile_composer_browser import live_hub  # noqa: F401
from test_workspace_shell_browser import login_page
from workspace_fixture import seed_workspace

playwright=pytest.importorskip('playwright.sync_api')
OUTPUT=Path(__file__).resolve().parents[2]/'output/hub-hybrid-20261029'


@pytest.mark.parametrize('width',[1440,390])
def test_execution_labels_and_chat_record_survive_board_filter_and_live_refresh(live_hub,repo,width):
    data=seed_workspace(repo,room_messages=2)
    chat,project,direct=data['tasks'][:3]
    for task,route in ((chat,'chat'),(project,'project'),(direct,'direct')):
        repo.connection.execute('UPDATE tasks SET execution_route=? WHERE id=?',(route,task['id']))
    repo.connection.commit()
    message=repo.create_entry(data['project']['id'],'总控','message',
        '工作已安排，你可以继续补充意见。',task_id=chat['id'],author_device_id=data['connection']['id'])
    with playwright.sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':width,'height':960})
        page.set_default_timeout(10000)
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        try:
            login_page(page,live_hub)
            page.goto(f"{live_hub}/projects/workspace-team/conversations/{data['conversation_id']}")
            page.wait_for_function("document.querySelector('[data-ws-shell]').dataset.wsReady==='true'")
            page.locator('[data-ws-input]').fill('下一步请继续完善，保留这份草稿')
            entry=page.locator(f'[data-ws-entry="{message["id"]}"]')
            playwright.expect(entry).to_contain_text('你可以继续补充意见')
            page.locator('[data-ws-task-summary]').click()
            playwright.expect(page.locator(f'[data-ws-task-card="{chat["id"]}"]')).to_have_count(0)
            history=page.locator('[data-ws-task-view]').filter(has_text='历史任务')
            playwright.expect(history).to_have_text('历史任务 · 2')
            project_card=page.locator(f'[data-ws-task-card="{project["id"]}"]')
            direct_card=page.locator(f'[data-ws-task-card="{direct["id"]}"]')
            playwright.expect(project_card.locator('[data-execution-route]')).to_have_text('本地工程')
            playwright.expect(direct_card.locator('[data-execution-route]')).to_have_text('单次执行')

            # A live update must replace the card label without navigation or
            # losing the input draft, just like incoming messages do.
            page.evaluate('window.__executionRouteDocument = true')
            repo.connection.execute("UPDATE tasks SET execution_route='project',execution_route_reason='接续本地工程' WHERE id=?",(direct['id'],))
            repo.connection.commit()
            playwright.expect(direct_card.locator('[data-execution-route]')).to_have_text('本地工程',timeout=12000)
            assert page.evaluate('window.__executionRouteDocument')
            assert page.locator('[data-ws-input]').input_value()=='下一步请继续完善，保留这份草稿'

            history.click()
            playwright.expect(page.locator(f'[data-ws-task-card="{chat["id"]}"]')).to_have_count(0)
            playwright.expect(page.locator('[data-ws-task-card]')).to_have_count(2)
            playwright.expect(entry).to_contain_text('你可以继续补充意见')
            entry.locator('[data-ws-task]').click()
            chat_panel=page.locator(f'[data-ws-task-panel="{chat["id"]}"]')
            playwright.expect(chat_panel).to_be_visible()
            playwright.expect(chat_panel.locator('[data-execution-route]')).to_have_text('聊天协调')
            OUTPUT.mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(OUTPUT/f'execution-routes-chat-{width}.png'))
            chat_panel.locator('.ws-full-link').click()
            playwright.expect(page.locator('[data-execution-route="chat"]')).to_have_text('聊天协调')
            assert not errors
        finally:
            browser.close()
