"""Settings stay inside an isolated workspace, including cached/offline documents."""
import json
from pathlib import Path

import pytest
from test_mobile_composer_browser import live_hub  # noqa: F401
from test_workspace_shell_browser import login_page
from workspace_fixture import seed_workspace

playwright = pytest.importorskip('playwright.sync_api')
OUTPUT = Path(__file__).resolve().parents[2] / 'output/hub-settings-motion-20261117/web'


@pytest.fixture
def settings_page(live_hub, repo):
    data = seed_workspace(repo, room_messages=80)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':1440,'height':960})
        page.set_default_timeout(10000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        login_page(page, live_hub)
        page.goto(live_hub + f'/projects/workspace-team/conversations/{data["conversation_id"]}')
        playwright.expect(page.locator('[data-ws-shell]')).to_have_attribute('data-ws-ready', 'true')
        yield page, data
        assert not errors
        browser.close()


def open_settings(page):
    page.locator('[data-ws-settings]').click()
    playwright.expect(page.locator('#ws-settings')).to_be_visible()


def test_settings_history_draft_and_reading_position_do_not_navigate(settings_page):
    page, data = settings_page
    page.locator('[data-ws-input]').fill('设置期间保留我的草稿\n以及第二行')
    page.locator('[data-ws-scroll]').evaluate('el => el.scrollTop = 180')
    before = page.locator('[data-ws-scroll]').evaluate('el => el.scrollTop')
    page.evaluate('window.__settingsDocumentIdentity = {}')
    requests = []
    page.on('request', lambda req: requests.append(req.url) if req.is_navigation_request() else None)
    open_settings(page)
    page.locator('[data-settings-tab="appearance"]').click()
    page.keyboard.press('ArrowLeft')
    playwright.expect(page.locator('[data-settings-tab="models"]')).to_be_focused()
    page.locator('[data-settings-tab="appearance"]').click()
    # Repeated entry calls are idempotent and keep one history boundary.
    page.evaluate("HubWorkspaceSettings.open('appearance'); HubWorkspaceSettings.open('appearance')")
    page.go_back()
    playwright.expect(page.locator('#ws-settings')).not_to_be_visible()
    assert '#settings=' not in page.url
    assert page.locator('[data-ws-input]').input_value() == '设置期间保留我的草稿\n以及第二行'
    assert abs(page.locator('[data-ws-scroll]').evaluate('el => el.scrollTop') - before) <= 1
    assert page.evaluate('Boolean(window.__settingsDocumentIdentity)')
    page.go_forward()
    playwright.expect(page.locator('[data-settings-panel="appearance"]')).to_be_visible()
    page.keyboard.press('Escape')
    playwright.expect(page.locator('#ws-settings')).not_to_be_visible()
    playwright.expect(page.locator('[data-ws-settings]')).to_be_focused()
    assert requests == []


@pytest.mark.parametrize('width', [1440, 390])
def test_settings_refresh_deeplink_and_reduced_motion(settings_page, width):
    page, _ = settings_page
    page.set_viewport_size({'width':width,'height':900})
    page.goto(page.url + '#settings=appearance')
    playwright.expect(page.locator('#ws-settings')).to_be_visible()
    page.reload()
    playwright.expect(page.locator('[data-settings-panel="appearance"]')).to_be_visible()
    page.locator('[data-settings-motion]').select_option('reduce')
    assert page.evaluate("document.documentElement.dataset.hubMotion") == 'reduce'
    assert page.locator('#ws-settings').evaluate('el => el.getAnimations({subtree:true}).length') == 0
    page.locator('[data-settings-tab="project"]').click()
    assert page.locator('#ws-settings').evaluate('el => el.getAnimations({subtree:true}).length') == 0
    page.locator('[data-settings-tab="appearance"]').click()
    page.locator('[data-settings-motion]').select_option('system')
    page.emulate_media(reduced_motion='reduce')
    playwright.expect(page.locator('html')).to_have_attribute('data-hub-motion', 'reduce')
    assert page.locator('#ws-settings').evaluate('el => el.getAnimations({subtree:true}).length') == 0
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUTPUT / f'settings-appearance-{width}.png'))


def test_offline_and_html_api_response_keep_settings_local(settings_page):
    page, _ = settings_page
    page.route('**/api/projects/*/settings', lambda route: route.fulfill(status=200, content_type='text/html', body='<html>old settings page</html>'))
    open_settings(page)
    page.locator('[data-settings-tab="connections"]').click()
    playwright.expect(page.locator('[data-settings-connection-status]')).to_contain_text('暂不可用')
    assert 'old settings page' not in page.locator('#ws-settings').inner_text()
    page.context.set_offline(True)
    page.locator('[data-settings-tab="models"]').click()
    playwright.expect(page.locator('[data-settings-model-status]')).to_contain_text('离线')
    page.locator('[data-settings-tab="appearance"]').click()
    page.locator('[data-settings-motion]').select_option('reduce')
    page.keyboard.press('Escape')
    playwright.expect(page.locator('#ws-settings')).not_to_be_visible()
    open_settings(page)
    assert 'view=archive' not in page.url
    page.context.set_offline(False)


def test_old_settings_anchors_open_modern_surface(settings_page):
    page, _ = settings_page
    base = page.url.split('/conversations/')[0]
    page.goto(base + '#members')
    playwright.expect(page.locator('[data-settings-panel="connections"]')).to_be_visible()
    assert 'view=archive' not in page.url
    page.goto(base + '?view=archive#members')
    playwright.expect(page.locator('[data-settings-panel="connections"]')).to_be_visible()
    assert 'view=archive' not in page.url
    page.goto(base + '#control')
    playwright.expect(page.locator('[data-settings-panel="connections"]')).to_be_visible()
    assert 'view=archive' not in page.url


def test_packaged_old_snapshot_repairs_settings_and_preserves_messages(settings_page):
    import re
    from codex_bridge.desktop_assets import PackagedAssets, scrub_credentials
    from codex_bridge.desktop_window import HubLocation
    page, _ = settings_page
    url = page.url
    # Real server source (before browser-created dialog) models a previous cache.
    source = page.request.get(url).text()
    source = re.sub(r'<(?:script|link)[^>]*workspace-settings[^>]*>(?:</script>)?', '', source)
    source = source.replace('href="#settings=project" data-ws-settings aria-haspopup="dialog" aria-controls="ws-settings"',
                            'href="/projects/workspace-team?view=archive#members"')
    source = scrub_credentials(source)
    location = HubLocation(url.split('/projects/')[0])
    prepared = PackagedAssets().prepare_snapshot(location, {'source_html':source,'html':source,'url':url})
    assert prepared['upgraded'] and not prepared['read_only']
    assert 'workspace-settings.js' in prepared['html']
    page.route(url, lambda route: route.fulfill(content_type='text/html', body=prepared['html']))
    page.reload()
    playwright.expect(page.locator('[data-ws-shell]')).to_have_attribute('data-ws-ready', 'true')
    count = page.locator('[data-ws-entry]').count()
    assert count > 0
    open_settings(page)
    page.locator('[data-settings-tab="appearance"]').click()
    assert page.locator('[data-ws-entry]').count() == count
    assert 'view=archive' not in page.url


def test_model_service_form_has_real_execution_scope_and_never_caches_key(settings_page):
    page, _ = settings_page
    requests = []
    services = []
    def service_api(route):
        if route.request.method == 'POST':
            data = route.request.post_data_json
            requests.append(data)
            services.append({**data, 'id':4, 'has_api_key':True})
            services[-1].pop('api_key')
        route.fulfill(content_type='application/json', body=json.dumps({'services':services, 'devices':[{'device_id':7,'device_label':'测试电脑','is_agent_online':True}], 'can_manage':True}))
    page.route('**/api/projects/*/model-services', service_api)
    open_settings(page)
    page.locator('[data-settings-tab="models"]').click()
    playwright.expect(page.locator('[data-settings-model-status]')).to_contain_text('已更新')
    page.locator('[data-settings-add-service]').click()
    form = page.locator('[data-settings-service-form]')
    form.locator('[name="name"]').fill('本机推理')
    form.locator('[name="kind"]').select_option('ollama')
    playwright.expect(form.locator('[name="base_url"]')).to_have_value('http://127.0.0.1:11434/v1')
    form.locator('[name="device_id"]').select_option('7')
    form.locator('[name="model"]').fill('test-model')
    form.locator('[name="api_key"]').fill('isolated-test-secret-never-cache')
    form.locator('[name="supports_images"]').check()
    form.locator('button[type="submit"]').click()
    playwright.expect(form).not_to_be_visible()
    assert requests == [{'name':'本机推理','kind':'ollama','device_id':7,'base_url':'http://127.0.0.1:11434/v1','model':'test-model','api_key':'isolated-test-secret-never-cache','supports_images':True,'enabled':True,'supports_tools':False,'clear_api_key':False}]
    assert form.locator('[name="api_key"]').input_value() == ''
    assert 'isolated-test-secret-never-cache' not in page.content()
    assert 'isolated-test-secret-never-cache' not in page.evaluate('JSON.stringify({...localStorage,...sessionStorage})')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUTPUT / 'settings-models-1440.png'))


def test_real_model_service_and_identity_policy_can_be_saved_in_place(settings_page, repo):
    page, data = settings_page
    token = repo.create_api_token(device_id=data['connection']['id'], name='settings-ui-isolated')
    response = page.request.post(page.url.split('/projects/')[0] + '/api/device/capabilities',
        headers={'Authorization':'Bearer ' + token.raw}, data={'model_services':True, 'execution_backend':'codex'})
    assert response.ok
    open_settings(page)
    page.locator('[data-settings-tab="models"]').click()
    playwright.expect(page.locator('[data-settings-model-status]')).to_contain_text('已更新')
    page.locator('[data-settings-add-service]').click()
    form = page.locator('[data-settings-service-form]')
    form.locator('[name="name"]').fill('隔离设置回归服务')
    form.locator('[name="kind"]').select_option('lmstudio')
    form.locator('[name="device_id"]').select_option(str(data['connection']['id']))
    form.locator('[name="model"]').fill('isolated-model')
    form.locator('[name="api_key"]').fill('isolated-ui-secret')
    form.locator('button[type="submit"]').click()
    playwright.expect(form).not_to_be_visible()
    row = repo.connection.execute('SELECT * FROM hub_model_services').fetchone()
    assert row and 'isolated-ui-secret' not in row['config_json']
    page.locator('[data-settings-services]').get_by_role('button', name='测试', exact=True).click()
    playwright.expect(page.locator('[data-settings-model-status]')).to_contain_text('已安排')
    assert repo.connection.execute('SELECT state FROM hub_model_service_probes').fetchone()[0] == 'queued'
    page.locator('[data-settings-agent-policy] summary').click()
    page.locator('[data-settings-policy-agent]').select_option(str(data['engineer']['id']))
    policy = page.locator('[data-settings-policy-form]')
    playwright.expect(policy).to_be_visible()
    policy.locator('[name="mode"]').select_option('manual')
    policy.locator('[name="model"]').select_option('hub-service/' + str(row['id']))
    playwright.expect(policy.locator('[name="reasoning_effort"]')).to_have_value('none')
    policy.locator('button[type="submit"]').click()
    playwright.expect(page.locator('[data-settings-policy-status]')).to_contain_text('新开始的工作')
    assert repo.connection.execute('SELECT model FROM hub_agent_model_policies WHERE agent_id=?', (data['engineer']['id'],)).fetchone()[0] == 'hub-service/' + str(row['id'])
    policy.locator('[name="mode"]').select_option('controller')
    policy.locator('button[type="submit"]').click()
    playwright.expect(page.locator('[data-settings-policy-status]')).to_contain_text('新开始的工作')
    assert repo.connection.execute('SELECT mode FROM hub_agent_model_policies WHERE agent_id=?', (data['engineer']['id'],)).fetchone()[0] == 'controller'
    assert 'view=archive' not in page.url
    OUTPUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUTPUT / 'settings-model-policy-real-1440.png'))


def test_motion_cancels_rapid_transitions_without_moving_messages(settings_page):
    page, _ = settings_page
    result = page.evaluate('''() => {
      const messages = document.querySelector('[data-ws-scroll]');
      const before = {top:messages.scrollTop,height:messages.scrollHeight};
      HubWorkspaceSettings.open('project');
      const initial = document.querySelector('#ws-settings').getAnimations({subtree:true}).length;
      for (const name of ['appearance','project','appearance','project']) document.querySelector('[data-settings-tab="'+name+'"]').click();
      const active = document.querySelector('#ws-settings').getAnimations({subtree:true}).filter(animation => !(animation instanceof CSSTransition)).length;
      HubWorkspaceSettings.close();
      return {before,after:{top:messages.scrollTop,height:messages.scrollHeight},initial,active,
        final:document.querySelector('#ws-settings').getAnimations({subtree:true}).length};
    }''')
    assert result['initial'] >= 1
    assert result['active'] <= 2  # Only the dialog and the latest active panel may move.
    assert result['final'] == 0
    assert result['before'] == result['after']


def test_unavailable_saved_policy_remains_visible_until_corrected(settings_page):
    page, data = settings_page
    page.route('**/api/projects/*/settings/agents/*/model-policy', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({'policy':{'mode':'manual','model':'hub-service/999','reasoning_effort':'none'}, 'models':[]})))
    open_settings(page)
    page.locator('[data-settings-tab="models"]').click()
    page.locator('[data-settings-agent-policy] summary').click()
    page.locator('[data-settings-policy-agent]').select_option(str(data['engineer']['id']))
    form = page.locator('[data-settings-policy-form]')
    playwright.expect(form).to_be_visible()
    playwright.expect(form.locator('[name="model"]')).to_have_value('hub-service/999')
    assert not form.locator('[name="model"]').evaluate('el => el.checkValidity()')
    playwright.expect(page.locator('[data-settings-policy-status]')).to_contain_text('已保留原手动选择')
    form.locator('[name="mode"]').select_option('controller')
    assert form.evaluate('el => el.checkValidity()')
