"""Workspace feedback and identity context remain real and draft-safe."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from test_mobile_composer_browser import live_hub  # noqa: F401
from test_workspace_shell_browser import login_page, assert_composer_visible
from workspace_fixture import seed_workspace

playwright = pytest.importorskip('playwright.sync_api')
OUTPUT = Path(__file__).resolve().parents[2] / 'output/hub-workspace-polish-20261027'


def within_viewport(page):
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')


@pytest.mark.parametrize('width', [1440, 1280, 390])
def test_feedback_context_keeps_chat_files_and_records_real_feedback(live_hub, repo, width):
    data = seed_workspace(repo, room_messages=8, engineer_name='Hub 工程师')
    repo.connection.execute('INSERT INTO hub_agent_connections VALUES (?)', (data['connection']['id'],))
    repo.connection.execute('INSERT OR REPLACE INTO hub_task_model_choices VALUES (?,?,?)', (data['tasks'][2]['id'],
        json.dumps({'model':'gpt-6-sol','reasoning_effort':'medium','source':'controller_task_plan','reason':'此工作按总控选择执行','policy_revision':1}), data['tasks'][2]['created_at']))
    repo.connection.commit()
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width':width, 'height':960}, timezone_id='Asia/Shanghai')
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            login_page(page, live_hub)
            OUTPUT.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(OUTPUT / f'home-{width}.png'))
            playwright.expect(page.locator('[data-ws-recipient-status]')).to_contain_text('离线')
            if width < 900:
                page.locator('[data-ws-sidebar-open]').click()
            engineer = page.locator(f'[data-ws-agent-dock] [data-ws-agent="{data["engineer"]["id"]}"]')
            assert engineer.locator('.ws-agent-name').inner_text() == 'Hub 工程师'
            assert engineer.locator('.ws-agent-state').inner_text() == '连接离线'
            engineer.click()
            if width < 1200:
                page.keyboard.press('Escape')
            page.locator('[data-ws-input]').fill('反馈在新页面打开，不应丢掉当前工作。')
            page.locator('[data-ws-attachment-input]').set_input_files({'name':'待发送资料.txt', 'mimeType':'text/plain', 'buffer':b'keep this draft'})
            feedback_link = page.locator('.ws-head-feedback')
            query = parse_qs(urlparse(feedback_link.get_attribute('href')).query)
            assert query['agent_id'] == [str(data['engineer']['id'])]
            assert 'conversation_id' not in query and 'task_id' not in query
            before_tasks = len(repo.list_tasks(data['project']['id']))
            with page.expect_popup() as popup_info:
                feedback_link.click()
            feedback = popup_info.value
            feedback.set_default_timeout(10000)
            feedback.wait_for_load_state('domcontentloaded')
            assert feedback.locator('[data-feedback-form] [name="agent_id"]').input_value() == str(data['engineer']['id'])
            assert feedback.locator('[name="viewport_width"]').input_value() == str(width)
            assert feedback.locator('[name="page_url"]').input_value().endswith('/projects/workspace-team')
            playwright.expect(page.locator('[data-ws-attachment]')).to_have_count(1)
            assert page.locator('[data-ws-input]').input_value() == '反馈在新页面打开，不应丢掉当前工作。'
            within_viewport(feedback)
            feedback.screenshot(path=str(OUTPUT / f'feedback-empty-{width}.png'), full_page=True)
            feedback.locator('[name="kind"]').select_option('idea')
            feedback.locator('[name="title"]').fill('让等待连接的工作更容易找到')
            feedback.locator('[name="body"]').fill('希望在当前工作里清楚看到哪些任务还在等待执行连接，恢复后也能找到原记录。')
            feedback.locator('[name="expected"]').fill('能够跟踪原任务，避免重复安排。')
            assert '我希望' in feedback.locator('[name="body"]').get_attribute('placeholder')
            feedback.evaluate('window.scrollTo(0,0)')
            feedback.screenshot(path=str(OUTPUT / f'feedback-filled-{width}.png'), full_page=True)
            with feedback.expect_navigation(wait_until='domcontentloaded'):
                feedback.locator('[data-feedback-form] button[type="submit"]').click()
            playwright.expect(feedback.locator('[data-feedback-id]')).to_have_count(1)
            assert len(repo.list_tasks(data['project']['id'])) == before_tasks
            record = repo.connection.execute('SELECT * FROM hub_feedback WHERE project_id=?', (data['project']['id'],)).fetchone()
            assert record['kind'] == 'idea' and record['title'] == '让等待连接的工作更容易找到'
            assert str(data['engineer']['id']) in record['context_json']
            within_viewport(feedback)
            feedback.evaluate('window.scrollTo(0,0)')
            feedback.screenshot(path=str(OUTPUT / f'feedback-list-{width}.png'), full_page=True)
            # Native 422 keeps the submitted fields and presents a usable error.
            feedback.locator('[name="title"]').fill(' ')
            feedback.locator('[name="body"]').fill('这段错误草稿需要保留。')
            with feedback.expect_navigation(wait_until='domcontentloaded'):
                feedback.locator('[data-feedback-form] button[type="submit"]').click()
            playwright.expect(feedback.locator('[role="alert"]')).to_be_visible()
            assert feedback.locator('[name="body"]').input_value() == '这段错误草稿需要保留。'
            playwright.expect(feedback.locator('[data-feedback-form] button[type="submit"]')).to_be_enabled()
            within_viewport(feedback)
            feedback.screenshot(path=str(OUTPUT / f'feedback-error-{width}.png'), full_page=True)
            feedback.goto(live_hub + '/projects/workspace-team/maintenance')
            within_viewport(feedback)
            feedback.screenshot(path=str(OUTPUT / f'maintenance-{width}.png'), full_page=True)
            feedback.close()

            page.locator('[data-ws-attachment-remove]').click()
            if width < 900:
                page.locator('[data-ws-sidebar-open]').click()
            page.locator(f'[data-ws-room="{data["conversation_id"]}"]').click()
            task = data['tasks'][2]
            page.locator(f'[data-ws-entry="{data["last_entry_id"]}"] [data-ws-task]').click()
            model = page.locator(f'[data-ws-task-panel="{task["id"]}"] [data-ws-model-choice]')
            assert 'gpt-6-sol' in model.inner_text() and '中档思考' in model.inner_text() and '总控按任务选择' in model.inner_text()
            if width < 1200:
                page.keyboard.press('Escape')
            query = parse_qs(urlparse(page.locator('.ws-head-feedback').get_attribute('href')).query)
            assert query['task_id'] == [str(task['id'])]
            assert query['agent_id'] == [str(data['reviewer']['id'])]
            assert query['conversation_id'] == [str(data['conversation_id'])]
            clock = page.locator(f'[data-ws-entry="{data["last_entry_id"]}"] time')
            assert '今天' in clock.inner_text() and clock.get_attribute('datetime') and clock.get_attribute('title')
            assert_composer_visible(page, 960)
            page.screenshot(path=str(OUTPUT / f'conversation-{width}.png'))
            page.goto(live_hub + f'/projects/workspace-team/tasks/{task["id"]}')
            base_feedback = page.locator('[data-project-feedback]')
            query = parse_qs(urlparse(base_feedback.get_attribute('href')).query)
            assert query['task_id'] == [str(task['id'])]
            within_viewport(page)
            assert not errors
        finally:
            browser.close()


def test_empty_workspace_feedback_has_no_invented_identity(live_hub, repo):
    project = repo.create_project(slug='empty-polish', name='新的协作项目')
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':1440, 'height':960})
        try:
            login_page(page, live_hub)
            page.goto(live_hub + '/projects/' + project['slug'])
            playwright.expect(page.locator('[data-ws-send]')).to_be_disabled()
            assert page.locator('[data-ws-agent-dock] [data-ws-agent]').count() == 0
            query = parse_qs(urlparse(page.locator('.ws-head-feedback').get_attribute('href')).query)
            assert not {'agent_id','task_id','conversation_id'} & query.keys()
            within_viewport(page)
            OUTPUT.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(OUTPUT / 'empty-team-1440.png'))
        finally:
            browser.close()
