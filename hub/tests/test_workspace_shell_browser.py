"""Real browser checks against an isolated local Hub with no executor workers."""
from pathlib import Path

import pytest
from test_mobile_composer_browser import live_hub  # noqa: F401
from workspace_fixture import seed_workspace

playwright = pytest.importorskip('playwright.sync_api')
OUTPUT = Path(__file__).resolve().parents[2] / 'output/hub-workspace-shell-20261027'


def login_page(page, base):
    page.goto(base + '/login')
    page.fill('[name="username"]', 'owner')
    page.fill('[name="password"]', 'owner-password')
    page.click('button[type="submit"]')
    playwright.expect(page.locator('[data-ws-shell]')).to_be_visible()
    playwright.expect(page.locator('[data-ws-shell]')).to_have_attribute('data-ws-ready', 'true')


def checked_identities(page):
    return page.locator('[data-ws-dispatch] [name="agent_ids"]:checked').evaluate_all('els => els.map(el => el.value)')


def assert_composer_visible(page, height):
    bounds = page.locator('[data-ws-input]').bounding_box()
    assert bounds and bounds['y'] >= 0 and bounds['y'] + bounds['height'] <= height + 1
    assert page.locator('[data-ws-send]').bounding_box()['y'] < height
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')


@pytest.mark.parametrize('width', [1440, 1280, 1024])
def test_workspace_identity_inspector_markdown_and_actual_dispatch(live_hub, repo, width):
    data = seed_workspace(repo, room_messages=90)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':width,'height':960})
        page.set_default_timeout(10000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            login_page(page, live_hub)
            OUTPUT.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(OUTPUT / f'workspace-home-{width}.png'))
            dock = page.locator('[data-ws-agent-dock]')
            assert dock.locator('[data-ws-agent]').count() == 3
            assert '总控' in dock.inner_text() and '工程师' in dock.inner_text() and '审查员' in dock.inner_text()
            assert data['connection']['label'] not in dock.inner_text()
            assert checked_identities(page) == [str(data['controller']['id'])]
            initial_draft = '查看身份时，保留这份正在编写的工作草稿。'
            page.locator('[data-ws-input]').fill(initial_draft)
            before_tasks = len(repo.list_tasks(data['project']['id']))
            avatar = dock.locator(f'[data-ws-agent="{data["engineer"]["id"]}"]')
            avatar.click()
            playwright.expect(page.locator(f'[data-ws-agent-panel="{data["engineer"]["id"]}"]')).to_be_visible()
            assert page.locator('[data-ws-input]').input_value() == initial_draft
            assert checked_identities(page) == [str(data['controller']['id'])]
            assert len(repo.list_tasks(data['project']['id'])) == before_tasks
            if width < 1200:
                assert page.locator('[data-ws-inspector]').evaluate('el => el.contains(document.activeElement)')
                page.keyboard.press('Escape')
                playwright.expect(avatar).to_be_focused()
                assert page.locator('[data-ws-inspector]').get_attribute('aria-hidden') == 'true'
                assert page.locator('[data-ws-inspector]').evaluate('el => el.inert')
            else:
                page.locator('[data-ws-inspector-close]').click()
                assert page.locator('[data-ws-inspector]').get_attribute('aria-hidden') == 'true'
                page.locator('[data-ws-inspector-open]').click()
                playwright.expect(page.locator('[data-ws-inspector]')).to_be_visible()

            page.locator(f'[data-ws-room="{data["conversation_id"]}"]').click()
            page.wait_for_url(f'**/conversations/{data["conversation_id"]}')
            assert page.locator('[data-ws-entry]').count() <= 40
            markdown = page.locator('[data-ws-message-body]')
            playwright.expect(markdown.locator('h2', has_text='交付结论')).to_be_visible()
            link = markdown.get_by_role('link', name='查看交付', exact=True)
            assert link.get_attribute('href') == 'https://example.invalid/output'
            assert markdown.locator('[href^="javascript:"]').count() == 0
            assert page.evaluate('window.__unsafeExecuted === undefined')
            assert_composer_visible(page, 960)
            OUTPUT.mkdir(parents=True, exist_ok=True)
            link.scroll_into_view_if_needed()
            page.screenshot(path=str(OUTPUT / f'workspace-messages-{width}.png'))
            assert page.locator('[data-ws-task-list]').get_attribute('open') is None
            page.locator('[data-ws-task-summary]').click()
            statuses = {task['status']:task for task in data['tasks']}
            for status, label in [('blocked','遇到阻碍'),('review','待复核')]:
                task = statuses[status]
                card = page.locator(f'.ws-task-delivery[data-ws-task="{task["id"]}"]')
                assert label in card.inner_text()
                assert '已验收' not in card.inner_text()
            cancelled = page.locator(f'.ws-task-delivery[data-ws-task="{statuses["cancelled"]["id"]}"]')
            playwright.expect(cancelled).to_have_count(0)
            page.locator('[data-ws-task-view]').filter(has_text='历史任务').click()
            playwright.expect(cancelled).to_contain_text('已取消')
            assert '已验收' not in cancelled.inner_text()
            page.locator('[data-ws-task-view]').filter(has_text='待办与交付').click()
            playwright.expect(cancelled).to_have_count(0)
            blocked = statuses['blocked']
            page.locator(f'.ws-task-delivery[data-ws-task="{blocked["id"]}"]').click()
            panel = page.locator(f'[data-ws-task-panel="{blocked["id"]}"]')
            playwright.expect(panel).to_be_visible()
            assert '遇到阻碍' in panel.inner_text()
            assert '工程师' in panel.inner_text()
            if width < 1200:
                page.keyboard.press('Escape')
            page.locator('[data-ws-task-summary]').click()
            assert page.locator('[data-ws-task-list]').get_attribute('open') is None

            long_draft = '\n'.join(f'第 {line} 条补充工作要求' for line in range(25))
            page.locator('[data-ws-input]').fill(long_draft)
            page.locator('[data-ws-scroll]').evaluate('el => { el.scrollTop = 0; }')
            assert_composer_visible(page, 960)
            page.locator('[data-ws-scroll]').evaluate('el => { el.scrollTop = el.scrollHeight; }')
            assert_composer_visible(page, 960)
            page.locator('[data-ws-recipient-toggle]').click()
            page.locator(f'[data-ws-recipient][value="{data["controller"]["id"]}"]').uncheck()
            page.locator(f'[data-ws-recipient][value="{data["reviewer"]["id"]}"]').check()
            assert page.locator(f'[data-ws-recipient][value="{data["paused"]["id"]}"]').is_disabled()
            page.locator('[data-ws-recipient-toggle]').click()
            assert checked_identities(page) == [str(data['reviewer']['id'])]
            description = '只安排审查员核对本次网页交付，其他身份不应收到新任务。'
            page.locator('[data-ws-input]').fill(description)
            first_key = page.locator('[data-ws-dispatch] [name="request_key"]').input_value()
            if width == 1440:
                # A real background refresh must preserve an in-progress dispatch.
                with page.expect_response(lambda response: response.request.headers.get('if-none-match') and response.status == 200, timeout=15000):
                    repo.create_entry(data['project']['id'], source_label=data['connection']['label'], kind='message',
                        body_markdown='浏览器轮询新增进展，草稿与收件人仍需保留',
                        author_device_id=data['connection']['id'], task_id=data['tasks'][2]['id'])
                playwright.expect(page.locator('[data-ws-message-body]', has_text='浏览器轮询新增进展')).to_have_count(1)
                assert page.locator('[data-ws-input]').input_value() == description
                assert checked_identities(page) == [str(data['reviewer']['id'])]
                assert page.locator('[data-ws-dispatch] [name="request_key"]').input_value() == first_key
            OUTPUT.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(OUTPUT / f'workspace-{width}.png'))
            with page.expect_navigation(wait_until='domcontentloaded'):
                page.locator('[data-ws-send]').click()
            created = [task for task in repo.list_tasks(data['project']['id']) if task['description'] == description]
            assert len(created) == 1
            assert created[0]['assigned_agent_id'] == data['reviewer']['id']
            assert created[0]['assigned_device_id'] == data['connection']['id']
            assert created[0]['conversation_id'] == data['conversation_id']
            assert page.locator('[data-ws-input]').input_value() == ''
            second_key = page.locator('[data-ws-dispatch] [name="request_key"]').input_value()
            assert second_key != first_key
            page.locator('[data-ws-input]').fill('第二次派单使用新的请求编号，继续在这场对话工作。')
            with page.expect_navigation(wait_until='domcontentloaded'):
                page.locator('[data-ws-send]').click()
            subsequent = [task for task in repo.list_tasks(data['project']['id']) if task['description'].startswith('第二次派单')]
            assert len(subsequent) == 1
            assert subsequent[0]['conversation_id'] == data['conversation_id']
            assert page.locator('[data-ws-input]').input_value() == ''
            assert page.locator('[data-ws-dispatch] [name="request_key"]').input_value() not in {first_key, second_key}
            # New work and an existing room each need fresh successful-submission keys.
            page.locator('[data-ws-new-work]').click()
            page.locator('[data-ws-input]').fill('开始一个全新的网页测试工作对话。')
            new_key = page.locator('[data-ws-dispatch] [name="request_key"]').input_value()
            with page.expect_navigation(wait_until='domcontentloaded'):
                page.locator('[data-ws-send]').click()
            started = [task for task in repo.list_tasks(data['project']['id']) if task['description'] == '开始一个全新的网页测试工作对话。']
            assert len(started) == 1 and started[0]['conversation_id'] != data['conversation_id']
            page.locator('[data-ws-new-work]').click()
            assert page.locator('[data-ws-input]').input_value() == ''
            assert page.locator('[data-ws-dispatch] [name="request_key"]').input_value() != new_key
            page.locator('[data-ws-input]').fill('第二个全新工作应建立另一场对话。')
            with page.expect_navigation(wait_until='domcontentloaded'):
                page.locator('[data-ws-send]').click()
            started_again = [task for task in repo.list_tasks(data['project']['id']) if task['description'] == '第二个全新工作应建立另一场对话。']
            assert len(started_again) == 1 and started_again[0]['conversation_id'] != started[0]['conversation_id']
            assert_composer_visible(page, 960)
            assert not errors
        finally:
            browser.close()


def test_workspace_modal_duplicate_name_preserves_both_forms_and_drawer_focus(live_hub, repo):
    data = seed_workspace(repo, room_messages=2)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':1440,'height':960})
        page.set_default_timeout(10000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            login_page(page, live_hub)
            page.locator('[data-ws-input]').fill('创建成员失败不能丢掉主输入框的草稿')
            opener = page.locator('.ws-team .ws-section-label [data-agents-open]')
            opener.click()
            page.locator('[data-agent-create-open]').first.click()
            form = page.locator('[data-agent-create]')
            form.locator('[name="name"]').fill('工程师')
            form.locator('[name="job"]').fill('这份职责需要保留')
            form.locator('[name="instructions"]').fill('失败时也保留工作方式')
            form.locator('button[type="submit"]').click()
            playwright.expect(form.locator('[data-agent-form-error]')).to_be_visible()
            assert '名称已存在' in form.locator('[data-agent-form-error]').inner_text()
            assert form.locator('[name="name"]').input_value() == '工程师'
            assert form.locator('[name="job"]').input_value() == '这份职责需要保留'
            assert form.locator('[name="instructions"]').input_value() == '失败时也保留工作方式'
            assert page.url.endswith('/projects/workspace-team')
            assert len(data['store'].list(data['project']['id'])) == 4
            page.keyboard.press('Escape')
            playwright.expect(opener).to_be_focused()
            assert page.locator('[data-ws-input]').input_value() == '创建成员失败不能丢掉主输入框的草稿'
            assert checked_identities(page) == [str(data['controller']['id'])]
            # The narrow navigation drawer must return focus to its entry point.
            page.set_viewport_size({'width':820,'height':960})
            nav = page.locator('[data-ws-sidebar-open]')
            nav.click()
            playwright.expect(page.locator('[data-ws-sidebar]')).to_be_visible()
            assert page.locator('[data-ws-sidebar]').evaluate('el => el.contains(document.activeElement)')
            page.keyboard.press('Shift+Tab')
            assert page.locator('[data-ws-sidebar]').evaluate('el => el.contains(document.activeElement)')
            page.keyboard.press('Escape')
            playwright.expect(nav).to_be_focused()
            assert page.locator('[data-ws-sidebar]').get_attribute('aria-hidden') == 'true'
            assert page.locator('[data-ws-sidebar]').evaluate('el => el.inert')
            assert_composer_visible(page, 960)
            assert not errors
        finally:
            browser.close()


def test_workspace_responsive_inspector_preserves_only_explicit_user_preference(live_hub, repo):
    data = seed_workspace(repo, room_messages=2, engineer_name='Hub 工程师')
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':1440,'height':960})
        page.set_default_timeout(10000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            login_page(page, live_hub)
            inspector = page.locator('[data-ws-inspector]')
            playwright.expect(inspector).to_have_attribute('aria-hidden', 'false')
            name = page.locator(f'[data-ws-agent-dock] [data-ws-agent="{data["engineer"]["id"]}"] .ws-agent-name')
            assert name.inner_text() == 'Hub 工程师'
            lines = name.evaluate('''el => {
                const range = document.createRange(); range.selectNodeContents(el);
                const box = el.getBoundingClientRect();
                const rects = Array.from(range.getClientRects()).filter(rect => rect.width && rect.height);
                return { count: new Set(rects.map(rect => Math.round(rect.top))).size,
                    contained: rects.every(rect => rect.left >= box.left-1 && rect.right <= box.right+1 && rect.top >= box.top-1 && rect.bottom <= box.bottom+1) };
            }''')
            assert lines == {'count':2, 'contained':True}

            # Automatic narrow layout must not become a persisted user decision.
            page.set_viewport_size({'width':1024,'height':960})
            playwright.expect(inspector).to_have_attribute('aria-hidden', 'true')
            page.set_viewport_size({'width':1440,'height':960})
            playwright.expect(inspector).to_have_attribute('aria-hidden', 'false')
            page.set_viewport_size({'width':1024,'height':960})
            page.goto(f"{live_hub}/projects/workspace-team/conversations/{data['conversation_id']}")
            playwright.expect(inspector).to_have_attribute('aria-hidden', 'true')
            page.set_viewport_size({'width':1440,'height':960})
            playwright.expect(inspector).to_have_attribute('aria-hidden', 'false')

            # Closing deliberately persists across reload and both responsive widths.
            page.locator('[data-ws-inspector-close]').click()
            playwright.expect(inspector).to_have_attribute('aria-hidden', 'true')
            page.reload()
            playwright.expect(inspector).to_have_attribute('aria-hidden', 'true')
            for width in [1024, 1440]:
                page.set_viewport_size({'width':width,'height':960})
                playwright.expect(inspector).to_have_attribute('aria-hidden', 'true')
            page.locator('[data-ws-inspector-open]').click()
            playwright.expect(inspector).to_have_attribute('aria-hidden', 'false')
            page.set_viewport_size({'width':1024,'height':960})
            playwright.expect(inspector).to_have_attribute('aria-hidden', 'true')
            page.set_viewport_size({'width':1440,'height':960})
            playwright.expect(inspector).to_have_attribute('aria-hidden', 'false')
            page.reload()
            playwright.expect(inspector).to_have_attribute('aria-hidden', 'false')
            assert_composer_visible(page, 960)
            assert not errors
            OUTPUT.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(OUTPUT / 'workspace-responsive-preference-1440.png'))
        finally:
            browser.close()

