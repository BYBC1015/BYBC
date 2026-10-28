from datetime import datetime, timedelta, timezone

import pytest
from test_mobile_composer_browser import live_hub  # noqa: F401
from test_workspace_shell_browser import login_page,checked_identities
from workspace_fixture import seed_workspace

playwright=pytest.importorskip('playwright.sync_api')


@pytest.mark.parametrize('width,conversation',[(1440,False),(390,True)])
def test_presence_refresh_preserves_focus_choices_draft_and_attachments(live_hub,repo,width,conversation):
    data=seed_workspace(repo,room_messages=2)
    device=data['connection']['id'];agent=data['controller']['id']
    repo.connection.execute('INSERT INTO hub_agent_connections(device_id) VALUES (?)',(device,))
    repo.connection.commit()
    token=repo.create_api_token(device_id=device,name='Presence test')
    def expire():
        stamp=(datetime.now(timezone.utc)-timedelta(seconds=130)).isoformat()
        repo.connection.execute('UPDATE devices SET last_agent_seen_at=? WHERE id=?',(stamp,device))
        repo.connection.commit()
    expire()
    with playwright.sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        context=browser.new_context(viewport={'width':width,'height':960})
        page=context.new_page();page.set_default_timeout(10000)
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        try:
            login_page(page,live_hub)
            if conversation:page.goto(f"{live_hub}/projects/workspace-team/conversations/{data['conversation_id']}")
            page.evaluate('window.__presenceDocument = true')
            page.locator('[data-ws-input]').fill('在线状态变化不能丢掉这份草稿')
            page.locator('[data-ws-attachment-input]').set_input_files({'name':'unsent.txt','mimeType':'text/plain','buffer':b'keep this file'})
            selected=checked_identities(page)
            task_count=len(repo.list_tasks(data['project']['id']))
            avatar=page.locator(f'[data-ws-agent-dock] [data-ws-agent="{agent}"]')
            label=avatar.locator('.ws-agent-state')
            playwright.expect(label).to_have_text('连接离线')
            if width<1200:page.locator('[data-ws-sidebar-open]').click()
            avatar.focus()
            def heartbeat():
                response=page.request.post(live_hub+'/api/device/heartbeat',headers={'Authorization':'Bearer '+token.raw})
                assert response.ok
            heartbeat()
            playwright.expect(label).to_have_text('已接入')
            playwright.expect(avatar.locator('.ws-agent-presence')).to_have_class('ws-agent-presence ws-agent-presence-connected')
            playwright.expect(avatar).to_be_focused()
            playwright.expect(page.locator('[data-ws-recipient-status]')).to_be_hidden()
            avatar.click()
            panel=page.locator(f'[data-ws-agent-panel="{agent}"]')
            panel.locator('.ws-full-link').focus()
            expire()
            playwright.expect(label).to_have_text('连接离线')
            playwright.expect(panel.locator('.ws-identity-status')).to_contain_text('离线')
            playwright.expect(panel.locator('.ws-full-link')).to_be_focused()
            if width<1200:
                page.keyboard.press('Escape')
                page.locator('[data-ws-sidebar-open]').click()
            page.locator('.ws-team .ws-section-label [data-agents-open]').click()
            card=page.locator(f'[data-agent-card="{agent}"]')
            choice=card.locator('[data-agent-select]');choice.check();choice.focus()
            heartbeat()
            playwright.expect(card.locator('.agent-presence')).to_contain_text('已接入')
            playwright.expect(choice).to_be_checked();playwright.expect(choice).to_be_focused()
            page.keyboard.press('Escape')
            context.set_offline(True)
            playwright.expect(label).to_have_text('状态待确认',timeout=15000)
            context.set_offline(False)
            playwright.expect(label).to_have_text('已接入',timeout=15000)
            assert page.locator('[data-ws-input]').input_value()=='在线状态变化不能丢掉这份草稿'
            assert 'unsent.txt' in page.locator('[data-ws-attachments]').inner_text()
            assert checked_identities(page)==selected
            assert len(repo.list_tasks(data['project']['id']))==task_count
            assert page.evaluate('window.__presenceDocument')
            assert not errors
        finally:browser.close()
