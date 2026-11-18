"""Real local-only settings UI coverage for the controller's optional reserve."""
from pathlib import Path

import pytest

from test_workspace_settings_motion_browser import settings_page, open_settings  # noqa: F401
from test_mobile_composer_browser import live_hub  # noqa: F401

playwright = pytest.importorskip('playwright.sync_api')
OUTPUT = Path('output/workspace-library-reserve-browser')


def test_controller_reserve_select_reload_clear_without_dispatch(settings_page, repo):
    page, data = settings_page
    base = page.url.split('/projects/')[0]
    token = repo.create_api_token(device_id=data['connection']['id'], name='reserve-ui-isolated')
    response = page.request.post(base + '/api/device/capabilities',
        headers={'Authorization':'Bearer ' + token.raw},
        data={'model_services':True, 'execution_backend':'codex'})
    assert response.ok
    task_count = repo.connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
    page.locator('[data-ws-input]').fill('备用模型设置期间保留的草稿')
    open_settings(page)
    page.locator('[data-settings-tab="models"]').click()
    playwright.expect(page.locator('[data-settings-model-status]')).to_contain_text('已更新')
    page.locator('[data-settings-add-service]').click()
    service = page.locator('[data-settings-service-form]')
    service.locator('[name="name"]').fill('隔离测试工具备用服务')
    service.locator('[name="kind"]').select_option('lmstudio')
    service.locator('[name="device_id"]').select_option(str(data['connection']['id']))
    service.locator('[name="model"]').fill('isolated-tools-model')
    service.locator('[name="supports_tools"]').check()
    service.locator('button[type="submit"]').click()
    playwright.expect(service).not_to_be_visible()
    row = repo.connection.execute('SELECT id,revision FROM hub_model_services').fetchone()
    reserve = f"hub-service/{row['id']}@{row['revision']}"
    page.locator('[data-settings-agent-policy] summary').click()
    picker = page.locator('[data-settings-policy-agent]')
    picker.select_option(str(data['engineer']['id']))
    policy = page.locator('[data-settings-policy-form]')
    playwright.expect(policy).to_be_visible()
    playwright.expect(page.locator('[data-settings-reserve]')).not_to_be_visible()
    picker.select_option(str(data['controller']['id']))
    playwright.expect(page.locator('[data-settings-reserve]')).to_be_visible()
    control = page.get_by_role('combobox', name='总控额度备用模型', exact=True)
    control.select_option(reserve)
    policy.locator('button[type="submit"]').click()
    playwright.expect(control).to_have_attribute('data-original', reserve)
    saved = page.request.get(base + f"/api/projects/workspace-team/settings/agents/{data['controller']['id']}/model-policy").json()
    assert saved['reserve'] == {'model':f"hub-service/{row['id']}", 'service_revision':row['revision']}
    assert saved['is_controller'] is True
    # Switching identities re-fetches the stored policy rather than retaining a select value.
    picker.select_option(str(data['engineer']['id']))
    playwright.expect(page.locator('[data-settings-reserve]')).not_to_be_visible()
    picker.select_option(str(data['controller']['id']))
    playwright.expect(control).to_have_value(reserve)
    control.scroll_into_view_if_needed()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUTPUT / 'controller-reserve-selected-1440.png'))
    control.select_option('')
    policy.locator('button[type="submit"]').click()
    playwright.expect(control).to_have_attribute('data-original', '')
    saved = page.request.get(base + f"/api/projects/workspace-team/settings/agents/{data['controller']['id']}/model-policy").json()
    assert saved['reserve'] is None
    assert repo.connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == task_count
    page.keyboard.press('Escape')
    playwright.expect(page.locator('#ws-settings')).not_to_be_visible()
    playwright.expect(page.locator('[data-ws-input]')).to_have_value('备用模型设置期间保留的草稿')
    assert 'view=archive' not in page.url
