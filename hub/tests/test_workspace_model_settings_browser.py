"""User model overrides are real, reversible, and do not rewrite started work."""
import json
from pathlib import Path

import httpx
import pytest
from test_mobile_composer_browser import live_hub  # noqa: F401
from test_workspace_shell_browser import login_page
from workspace_fixture import seed_workspace

playwright = pytest.importorskip('playwright.sync_api')
OUTPUT = Path(__file__).resolve().parents[2] / 'output/hub-workspace-polish-20261027'


def report_models(repo, live_hub, device):
    token = repo.create_api_token(device_id=device['id'], name='model-settings-browser')
    result = httpx.post(live_hub + '/api/device/capabilities',
        headers={'Authorization': 'Bearer ' + token.raw}, json={
            'execution_backend':'codex', 'codex':{'source':'device', 'models':[
                {'model':'gpt-5.6-sol', 'displayName':'GPT-5.6', 'supportedReasoningEfforts':[
                    {'reasoningEffort':'high'}]},
                {'model':'gpt-6-sol', 'displayName':'GPT-6', 'isDefault':True, 'supportedReasoningEfforts':[
                    {'reasoningEffort':'medium'}, {'reasoningEffort':'high'}]},
            ]}})
    assert result.status_code == 200


@pytest.mark.parametrize('width', [1440, 390])
def test_user_can_override_and_restore_controller_model_policy(live_hub, repo, width):
    data = seed_workspace(repo, room_messages=1)
    report_models(repo, live_hub, data['connection'])
    frozen = json.dumps({'model':'gpt-6-sol','reasoning_effort':'medium',
        'source':'controller_task_plan','reason':'按本次工作选择','policy_revision':1})
    repo.connection.execute('INSERT INTO hub_task_model_choices VALUES (?,?,?)',
        (data['tasks'][0]['id'], frozen, data['tasks'][0]['created_at']))
    repo.connection.commit()
    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':width,'height':960})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            login_page(page, live_hub)
            url = live_hub + f'/projects/workspace-team/agents/{data["engineer"]["id"]}'
            page.goto(url + '#agent-model-settings')
            form = page.locator('#agent-model-settings')
            assert form.locator('[name="mode"]').input_value() == 'controller'
            assert set(form.locator('[name="model"] option').evaluate_all('(items)=>items.map(x=>x.value)')) == {'','gpt-5.6-sol','gpt-6-sol'}
            form.locator('[name="mode"]').select_option('manual')
            form.locator('[name="model"]').select_option('gpt-5.6-sol')
            assert form.locator('[name="reasoning_effort"] option').evaluate_all('(items)=>items.map(x=>x.value)') == ['high']
            form.locator('[name="reasoning_effort"]').select_option('high')
            form.locator('button[type="submit"]').click()
            page.wait_for_url(url + '#agent-model-settings')
            playwright.expect(form.locator('[name="mode"]')).to_have_value('manual')
            playwright.expect(form.locator('[name="model"]')).to_have_value('gpt-5.6-sol')
            row = repo.connection.execute('SELECT * FROM hub_agent_model_policies WHERE agent_id=?',
                (data['engineer']['id'],)).fetchone()
            assert row['mode'] == 'manual' and row['model'] == 'gpt-5.6-sol' and row['reasoning_effort'] == 'high'
            page.reload()
            playwright.expect(form.locator('[name="model"]')).to_have_value('gpt-5.6-sol')
            form.scroll_into_view_if_needed()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            OUTPUT.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(OUTPUT / f'model-settings-manual-{width}.png'))
            form.locator('[name="model"]').select_option('gpt-6-sol')
            assert form.locator('[name="reasoning_effort"] option').evaluate_all('(items)=>items.map(x=>x.value)') == ['medium','high']
            form.locator('[name="reasoning_effort"]').select_option('medium')
            with page.expect_navigation(wait_until='domcontentloaded'):
                form.locator('button[type="submit"]').click()
            playwright.expect(form.locator('[name="model"]')).to_have_value('gpt-6-sol')
            form.locator('[name="mode"]').select_option('controller')
            with page.expect_navigation(wait_until='domcontentloaded'):
                form.locator('button[type="submit"]').click()
            playwright.expect(form.locator('[name="mode"]')).to_have_value('controller')
            row = repo.connection.execute('SELECT * FROM hub_agent_model_policies WHERE agent_id=?',
                (data['engineer']['id'],)).fetchone()
            assert row['mode'] == 'controller' and row['model'] == ''
            assert repo.connection.execute('SELECT selection_json FROM hub_task_model_choices WHERE task_id=?',
                (data['tasks'][0]['id'],)).fetchone()[0] == frozen
            form.scroll_into_view_if_needed()
            page.screenshot(path=str(OUTPUT / f'model-settings-controller-{width}.png'))
            assert not errors
        finally:
            browser.close()


def test_model_catalogue_change_keeps_invalid_choice_for_correction(live_hub, repo):
    data = seed_workspace(repo, room_messages=1)
    report_models(repo, live_hub, data['connection'])
    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':1440,'height':960})
        try:
            login_page(page, live_hub)
            page.goto(live_hub + f'/projects/workspace-team/agents/{data["engineer"]["id"]}#agent-model-settings')
            form = page.locator('#agent-model-settings')
            form.locator('[name="mode"]').select_option('manual')
            # A manual model is required without fabricating a choice.
            assert form.locator('[name="model"]').evaluate('(el)=>!el.checkValidity()')
            form.locator('[name="model"]').select_option('gpt-6-sol')
            form.locator('[name="reasoning_effort"]').select_option('high')
            token = repo.create_api_token(device_id=data['connection']['id'], name='catalogue-change-browser')
            response = httpx.post(live_hub + '/api/device/capabilities',
                headers={'Authorization':'Bearer ' + token.raw}, json={
                    'execution_backend':'codex', 'codex':{'models':[
                        {'model':'gpt-6-sol','displayName':'GPT-6',
                         'supportedReasoningEfforts':[{'reasoningEffort':'medium'}]}]}})
            assert response.status_code == 200
            with page.expect_navigation(wait_until='domcontentloaded') as navigation:
                form.locator('button[type="submit"]').click()
            assert navigation.value.status == 422
            playwright.expect(form.locator('[role="alert"]')).to_contain_text('不支持')
            playwright.expect(form.locator('[name="model"]')).to_have_value('gpt-6-sol')
            playwright.expect(form.locator('[name="reasoning_effort"]')).to_have_value('high')
            assert form.locator('[name="reasoning_effort"]').evaluate('(el)=>!el.checkValidity()')
            assert repo.connection.execute('SELECT count(*) FROM hub_agent_model_policies WHERE agent_id=?',
                (data['engineer']['id'],)).fetchone()[0] == 0
            form.scroll_into_view_if_needed()
            OUTPUT.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(OUTPUT / 'model-settings-error-1440.png'))
            form.locator('[name="reasoning_effort"]').select_option('medium')
            with page.expect_navigation(wait_until='domcontentloaded'):
                form.locator('button[type="submit"]').click()
            playwright.expect(form.locator('[role="alert"]')).to_have_count(0)
            row = repo.connection.execute('SELECT * FROM hub_agent_model_policies WHERE agent_id=?',
                (data['engineer']['id'],)).fetchone()
            assert row['mode'] == 'manual' and row['model'] == 'gpt-6-sol' and row['reasoning_effort'] == 'medium'
        finally:
            browser.close()


def test_model_settings_without_catalogue_can_keep_controller(live_hub, repo):
    data = seed_workspace(repo, room_messages=1)
    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':1440,'height':960})
        try:
            login_page(page, live_hub)
            page.goto(live_hub + f'/projects/workspace-team/agents/{data["engineer"]["id"]}#agent-model-settings')
            form = page.locator('#agent-model-settings')
            playwright.expect(form).to_contain_text('尚未上报模型目录')
            assert form.locator('[name="model"] option').count() == 1
            with page.expect_navigation(wait_until='domcontentloaded'):
                form.locator('button[type="submit"]').click()
            playwright.expect(form.locator('[name="mode"]')).to_have_value('controller')
            form.scroll_into_view_if_needed()
            OUTPUT.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(OUTPUT / 'model-settings-no-catalogue-1440.png'))
        finally:
            browser.close()
