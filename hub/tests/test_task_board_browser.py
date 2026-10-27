from pathlib import Path
import pytest
from test_mobile_composer_browser import live_hub
from test_workspace_shell_browser import login_page
from workspace_fixture import seed_workspace

playwright=pytest.importorskip('playwright.sync_api')


@pytest.mark.parametrize('width',[1440,390])
def test_archive_history_restore_and_finish_preserve_draft_and_file(live_hub,repo,width):
    d=seed_workspace(repo,room_messages=2);task_id=d['tasks'][2]['id']
    with playwright.sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':width,'height':950})
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        try:
            login_page(page,live_hub)
            page.goto(f"{live_hub}/projects/workspace-team/conversations/{d['conversation_id']}")
            page.locator('[data-ws-input]').fill('保留我正在写的内容')
            page.locator('[data-ws-attachment-input]').set_input_files({'name':'draft.txt','mimeType':'text/plain','buffer':b'unsent attachment'})
            page.locator('[data-ws-task-summary]').click()
            card=page.locator(f'[data-ws-task-card="{task_id}"]')
            card.locator('button[value="archive"]').click()
            playwright.expect(page.locator('[data-ws-task-feedback]')).to_contain_text('已归档',timeout=6000)
            playwright.expect(card).to_have_count(0,timeout=12000)
            assert repo.get_task(task_id)['status']=='review'
            page.locator('[data-ws-task-view]').filter(has_text='历史任务').click()
            playwright.expect(card).to_have_count(1,timeout=12000)
            card.locator('button[value="restore"]').click()
            playwright.expect(card).to_have_count(0,timeout=12000)
            page.locator('[data-ws-task-view]').filter(has_text='待办与交付').click()
            playwright.expect(card).to_have_count(1,timeout=12000)
            card.locator('button[value="complete"]').click()
            playwright.expect(card).to_have_count(0,timeout=12000)
            assert repo.get_task(task_id)['status']=='done'
            assert page.locator('[data-ws-input]').input_value()=='保留我正在写的内容'
            assert page.locator('[data-ws-attachments]').inner_text().count('draft.txt')==1
            assert not errors
            output=Path(__file__).resolve().parents[2]/'output/hub-task-board-20261028/browser'
            output.mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(output/f'task-board-{width}.png'))
        finally:browser.close()
