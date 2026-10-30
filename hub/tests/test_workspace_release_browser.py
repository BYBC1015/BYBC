"""Network recovery must upgrade the shell without losing unsubmitted work."""
import pytest
from codex_hub import __version__
from test_mobile_composer_browser import live_hub  # noqa: F401
from test_workspace_shell_browser import login_page
from workspace_fixture import seed_workspace

playwright = pytest.importorskip('playwright.sync_api')


def make_stale(page, version='2.9.5'):
    page.evaluate('(version) => { window.__oldShell = true; document.querySelector("[data-ws-shell]").dataset.wsRelease = version; window.dispatchEvent(new Event("online")); }', version)


def test_idle_shell_updates_after_reconnect(live_hub, repo):
    seed_workspace(repo, room_messages=2)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            login_page(page, live_hub)
            page.locator('[data-ws-input]').blur()
            make_stale(page)
            page.wait_for_function('!window.__oldShell && document.querySelector("[data-ws-shell]")?.dataset.wsReady === "true"')
            assert page.locator('[data-ws-shell]').get_attribute('data-ws-release') == __version__
        finally:
            browser.close()


def test_update_preserves_draft_and_attachment(live_hub, repo):
    data = seed_workspace(repo, room_messages=2)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width':390, 'height':844})
            login_page(page, live_hub)
            original_count = len(repo.list_tasks(data['project']['id']))
            page.locator('[data-ws-input]').fill('网络恢复后不要丢掉我正在写的追加要求')
            page.locator('[data-ws-attachment-input]').set_input_files({'name':'keep.txt', 'mimeType':'text/plain', 'buffer':b'important local attachment'})
            make_stale(page)
            notice = page.locator('[data-ws-release-update]')
            playwright.expect(notice).to_be_visible()
            assert page.evaluate('window.__oldShell')
            notice.get_by_role('button', name='载入新版').click()
            page.wait_for_function('!window.__oldShell && document.querySelector("[data-ws-shell]")?.dataset.wsReady === "true"')
            playwright.expect(page.locator('[data-ws-input]')).to_have_value('网络恢复后不要丢掉我正在写的追加要求')
            playwright.expect(page.locator('[data-ws-attachments]')).to_contain_text('keep.txt')
            scope = page.locator('[data-ws-shell]').evaluate('el=>`${el.dataset.user}.${el.dataset.project}.${el.dataset.conversation}`')
            saved = page.evaluate('async scope => { const {draft}=await CodexWorkspaceStore.read(scope);return await draft.files[0].file.text(); }', scope)
            assert saved == 'important local attachment'
            assert len(repo.list_tasks(data['project']['id'])) == original_count
        finally:
            browser.close()


def test_stale_server_cannot_downgrade_current_shell(live_hub, repo):
    seed_workspace(repo, room_messages=2)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            login_page(page, live_hub)
            make_stale(page, '99.0.0')
            playwright.expect(page.locator('[data-ws-shell]')).to_have_attribute('data-ws-connection','offline')
            assert page.evaluate('window.__oldShell')
            assert page.locator('[data-ws-shell]').get_attribute('data-ws-release') == '99.0.0'
            assert page.locator('[data-ws-release-update]').count() == 0
        finally:
            browser.close()


def test_unsaved_non_composer_edit_prevents_reload(live_hub, repo):
    seed_workspace(repo, room_messages=2)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width':1440, 'height':960})
            login_page(page, live_hub)
            page.locator('[data-ws-room-search]').fill('仍在筛选的对话')
            page.locator('[data-ws-room-search]').blur()
            make_stale(page)
            notice = page.locator('[data-ws-release-update]')
            playwright.expect(notice).to_be_visible()
            notice.get_by_role('button', name='载入新版').click()
            playwright.expect(notice.locator('span')).to_contain_text('尚未完成保存')
            assert page.evaluate('window.__oldShell')
            assert page.locator('[data-ws-room-search]').input_value() == '仍在筛选的对话'
        finally:
            browser.close()
