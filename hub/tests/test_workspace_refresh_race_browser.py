"""A late HTML response must never overwrite a newly selected task view."""
import pytest

from test_mobile_composer_browser import live_hub  # noqa: F401
from test_workspace_shell_browser import login_page
from workspace_fixture import seed_workspace

playwright = pytest.importorskip('playwright.sync_api')


def test_late_refresh_keeps_new_task_view_and_draft(live_hub, repo):
    data = seed_workspace(repo, room_messages=2)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            login_page(page, live_hub)
            page.goto(f'{live_hub}/projects/workspace-team/conversations/{data["conversation_id"]}')
            page.wait_for_function('document.querySelector("[data-ws-shell]")?.dataset.wsReady === "true"')
            page.locator('[data-ws-input]').fill('保留正在写的修改要求')
            page.locator('[data-ws-task-summary]').click()
            page.evaluate('''() => {
                const original = window.fetch;
                let held = false;
                const barrier = new Promise(resolve => window.releaseOldRefresh = resolve);
                window.refreshResults = [];
                addEventListener('hub:workspace-updated', () => {
                    window.refreshResults.push({
                        url: document.querySelector('[data-ws-shell]').dataset.workspaceUrl,
                        history: !!document.querySelector('.ws-task-delivery[data-ws-task="%s"]'),
                        connection: document.querySelector('[data-ws-shell]').dataset.wsConnection
                    });
                });
                window.fetch = async (url, options) => {
                    const response = await original(url, options);
                    if (!held && String(url).includes('/conversations/') && !String(url).includes('task_history') && options?.cache === 'no-store') {
                        held = true;
                        return new Proxy(response, {get(target, key) {
                            if (key === 'text') return async () => {
                                const text = await target.text();
                                window.oldRefreshHeld = true;
                                await barrier;
                                return text;
                            };
                            const value = Reflect.get(target, key, target);
                            return typeof value === 'function' ? value.bind(target) : value;
                        }});
                    }
                    return response;
                };
                dispatchEvent(new Event('online'));
            }''' % next(task['id'] for task in data['tasks'] if task['status'] == 'cancelled'))
            page.wait_for_function('window.oldRefreshHeld === true')
            page.locator('[data-ws-task-view]').filter(has_text='历史任务').click()
            page.evaluate('window.releaseOldRefresh()')
            page.wait_for_function('window.refreshResults.some(item => item.history)', timeout=5000)
            results = page.evaluate('window.refreshResults')
            assert all(item['history'] for item in results if 'task_history=1' in item['url']), results
            assert page.locator('[data-ws-input]').input_value() == '保留正在写的修改要求'
            assert page.locator('[data-ws-shell]').get_attribute('data-ws-connection') == 'online'
        finally:
            browser.close()
