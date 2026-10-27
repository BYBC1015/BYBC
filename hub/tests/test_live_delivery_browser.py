from pathlib import Path
import pytest
from test_mobile_composer_browser import live_hub
from test_workspace_shell_browser import login_page
from workspace_fixture import seed_workspace

playwright=pytest.importorskip('playwright.sync_api')
OUTPUT=Path(__file__).resolve().parents[2]/'output/hub-2.5.0-20261027/browser'


@pytest.mark.parametrize('width',[1440,390])
def test_new_messages_arrive_while_old_entry_focused_and_keep_draft(live_hub,repo,width):
    d=seed_workspace(repo,room_messages=70)
    with playwright.sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':width,'height':950})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        try:
            login_page(page,live_hub)
            page.goto(f"{live_hub}/projects/workspace-team/conversations/{d['conversation_id']}")
            page.wait_for_function("document.querySelector('[data-ws-shell]').dataset.wsReady==='true'")
            page.locator('[data-ws-input]').fill('我还在输入下一条修改')
            link=page.locator('[data-ws-entry] [data-ws-task]').first
            link.scroll_into_view_if_needed();link.focus()
            entry=repo.create_entry(d['project']['id'],'总控','message','总控的新回复：工作继续执行，你可以继续补充。',author_device_id=d['connection']['id'],task_id=d['tasks'][0]['id'])
            incoming=page.locator(f'[data-ws-entry="{entry["id"]}"]')
            playwright.expect(incoming).to_have_count(1,timeout=12000)
            playwright.expect(link).to_be_focused()
            assert page.locator('[data-ws-input]').input_value()=='我还在输入下一条修改'
            playwright.expect(page.locator('[data-ws-new-messages]')).to_be_visible()
            page.locator('[data-ws-new-messages]').click()
            playwright.expect(incoming).to_be_in_viewport()
            assert not page.locator('[data-ws-new-messages]').is_visible()
            # Existing message changes are delivered too, without manually refreshing.
            repo.connection.execute('UPDATE entries SET body_markdown=? WHERE id=?',('已接续修改并交付成果。',entry['id']));repo.connection.commit()
            playwright.expect(incoming).to_contain_text('已接续修改并交付成果',timeout=12000)
            OUTPUT.mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(OUTPUT/f'live-chat-{width}.png'))
            assert errors==[]
        finally:browser.close()


def test_pure_text_followup_displays_once_with_receipt(live_hub,repo):
    d=seed_workspace(repo,room_messages=2)
    with playwright.sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1440,'height':950})
        try:
            login_page(page,live_hub)
            page.goto(f"{live_hub}/projects/workspace-team/conversations/{d['conversation_id']}")
            page.locator('[data-ws-input]').fill('这个截图请直接处理，先调整背景')
            page.locator('[data-ws-send]').click()
            message=page.locator('[data-ws-entry]').filter(has_text='这个截图请直接处理，先调整背景')
            playwright.expect(message).to_have_count(1,timeout=15000)
            playwright.expect(message.locator('.ws-receipt')).to_contain_text('已收到')
            page.reload()
            playwright.expect(message).to_have_count(1)
            assert page.locator('.ws-user-message').count()==0 # obsolete separate purpose bubble
        finally:browser.close()
