"""Mobile geometry regressions in an isolated Hub; keyboard is simulated."""
import json
from pathlib import Path

import pytest

from test_mobile_composer_browser import live_hub  # noqa: F401
from test_workspace_shell_browser import login_page
from test_workspace_product_ui_browser import VISUAL_VIEWPORT
from test_desktop_progress_browser import open_room
from workspace_fixture import seed_workspace

playwright = pytest.importorskip('playwright.sync_api')
OUTPUT = Path(__file__).resolve().parents[2] / 'output/hub-mobile-local-20261031/ui'


def evidence(page, name):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUTPUT / (name + '.png')))
    data = page.evaluate('''() => ({
      viewport: {width:innerWidth,height:innerHeight,touch:matchMedia('(pointer:coarse)').matches},
      overflow: document.documentElement.scrollWidth > innerWidth + 1,
      layout: [...document.querySelectorAll('.ws-center,.ws-chat-head,.ws-chat-title,.ws-messages,.ws-composer-region,.ws-composer,.ws-attachments,.ws-composer textarea,.ws-composer-bottom,.ws-release-update,.ws-pending-submission')].filter(n=>!n.hidden).map(n=>({class:n.className,rect:n.getBoundingClientRect().toJSON()}))
    })''')
    (OUTPUT / (name + '.json')).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


@pytest.mark.parametrize('width,height', [(320, 640), (360, 780), (430, 932), (932, 430), (1024, 500)])
def test_mobile_portrait_and_touch_landscape_keep_one_workspace(live_hub, repo, width, height):
    data = seed_workspace(repo, room_messages=0)
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': width, 'height': height}, is_mobile=True, has_touch=True)
        try:
            login_page(page, live_hub)
            open_room(page, live_hub, data['conversation_id'])
            evidence(page, f'layout-{width}-{height}')
            assert page.locator('[data-ws-sidebar-open]').is_visible()
            assert not page.locator('[data-ws-sidebar]').is_visible()
            assert page.locator('.ws-chat-title').bounding_box()['width'] >= 95
            assert page.locator('[data-ws-scroll]').bounding_box()['height'] >= 100
            assert page.locator('[data-ws-send]').bounding_box()['y'] + 44 <= height
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            page.locator('[data-ws-sidebar-open]').click()
            playwright.expect(page.locator('[data-ws-sidebar]')).to_be_visible()
            assert page.locator('[data-ws-sidebar]').get_attribute('aria-hidden') == 'false'
            page.locator('[data-ws-sidebar-close]').click()
            page.locator('[data-ws-inspector-open]').click()
            playwright.expect(page.locator('[data-ws-inspector]')).to_be_visible()
            page.locator('[data-ws-inspector-close]').click()
            assert page.locator('[data-ws-input]').is_enabled()
            page.locator('[data-ws-sidebar-open]').click()
            page.locator('[data-ws-toolbox]').click()
            dialog = page.locator('[data-ws-toolbox-dialog]')
            playwright.expect(dialog).to_be_visible()
            assert dialog.bounding_box()['height'] <= height
            page.locator('[data-ws-toolbox-close]').click()
        finally:
            browser.close()


def test_small_keyboard_with_attachments_and_recovery_notices_keeps_chat_and_send(live_hub, repo):
    data = seed_workspace(repo, room_messages=8)
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 320, 'height': 640}, is_mobile=True, has_touch=True)
        page.add_init_script(VISUAL_VIEWPORT)
        try:
            login_page(page, live_hub)
            open_room(page, live_hub, data['conversation_id'])
            draft = '继续按原定要求完成，不要丢失这一条消息。'
            page.locator('[data-ws-input]').fill(draft)
            page.locator('[data-ws-attachment-input]').set_input_files([
                {'name': '第' + str(i) + '份很长的原始资料名称_手机拍摄_城市背景_最终定稿.txt', 'mimeType': 'text/plain', 'buffer': b'reference'}
                for i in range(6)
            ])
            page.locator('[data-ws-input]').focus()
            page.evaluate('window.__setViewport({height:330,offsetTop:12})')
            playwright.expect(page.locator('[data-ws-shell]')).to_have_attribute('data-ws-compact-viewport', 'true')
            # A flaky connection and a newly published release can coexist with
            # the user's attachment draft. Exercise their actual UI components.
            page.evaluate('''() => {
              const pending=document.querySelector('[data-ws-pending]'); pending.hidden=false;
              pending.querySelector('[data-ws-queue-cancel]').hidden=false;
              const banner=document.createElement('div'); banner.className='ws-release-update';
              banner.innerHTML='<div><strong>新版本已准备好</strong><p>网络恢复后继续使用当前对话，草稿已经保存。</p></div><button type="button">更新</button>';
              document.querySelector('.ws-composer-region').prepend(banner);
            }''')
            evidence(page, 'keyboard-320-notices-files')
            assert page.locator('[data-ws-scroll]').bounding_box()['height'] >= 48
            send = page.locator('[data-ws-send]').bounding_box()
            assert send['y'] >= 12 and send['y'] + send['height'] <= 342
            assert page.locator('[data-ws-input]').input_value() == draft
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            # All notices and remove controls remain reachable by scrolling the
            # constrained composer instead of expanding beyond the keyboard.
            page.locator('[data-ws-queue-cancel]').scroll_into_view_if_needed()
            assert page.locator('[data-ws-queue-cancel]').is_visible()
            page.locator('[data-ws-send]').scroll_into_view_if_needed()
            assert page.locator('[data-ws-send]').bounding_box()['y'] + 44 <= 342
            page.locator('[data-ws-recipient-toggle]').scroll_into_view_if_needed()
            page.locator('[data-ws-recipient-toggle]').click()
            menu = page.locator('.ws-recipient-menu')
            playwright.expect(menu).to_be_visible()
            bounds = menu.bounding_box()
            assert bounds['y'] >= 12 and bounds['y'] + bounds['height'] <= 342
            evidence(page, 'keyboard-320-recipient-menu')
            page.locator('[data-ws-recipient-toggle]').click()
            page.locator('[data-ws-input]').focus()
            page.evaluate('''() => {
              const error=document.querySelector('[data-ws-dispatch-error]');
              error.textContent='这条消息尚未发出，请保留草稿后重试。'; error.hidden=false;
            }''')
            error = page.locator('[data-ws-dispatch-error]')
            playwright.expect(error).to_be_visible()
            page.wait_for_function('document.querySelector("[data-ws-dispatch-error]").getBoundingClientRect().bottom <= 342')
            evidence(page, 'keyboard-320-send-error')
            assert page.locator('[data-ws-input]').input_value() == draft
        finally:
            browser.close()


def test_phone_code_block_accepts_horizontal_touch_scroll(live_hub, repo):
    data = seed_workspace(repo, room_messages=0)
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
        try:
            login_page(page, live_hub)
            open_room(page, live_hub, data['conversation_id'])
            # Exercise the same markdown container with one unbroken line. It
            # must scroll locally by touch without widening the whole page.
            page.locator('[data-ws-scroll]').evaluate('''node => {
              node.innerHTML='<div class="ws-message"><div class="ws-message-content"><div class="ws-message-prose"><div class="codex-markdown"><pre data-horizontal-code style="overflow-x:auto;max-width:100%;white-space:pre">' + 'long_column_value, '.repeat(40) + '</pre></div></div></div></div>';
            }''')
            code = page.locator('[data-horizontal-code]')
            assert code.evaluate('n=>n.scrollWidth>n.clientWidth')
            code.scroll_into_view_if_needed()
            page.wait_for_timeout(80)
            box = code.bounding_box()
            session = page.context.new_cdp_session(page)
            x, y = box['x'] + box['width'] - 20, box['y'] + box['height']/2
            session.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [{'x': x, 'y': y}]})
            for step in range(1, 10):
                session.send('Input.dispatchTouchEvent', {'type': 'touchMove', 'touchPoints': [{'x': x - step * 18, 'y': y}]})
                page.wait_for_timeout(20)
            session.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
            page.wait_for_timeout(150)
            assert code.evaluate('n=>n.scrollLeft') > 40
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        finally:
            browser.close()


def test_short_mouse_desktop_preserves_side_navigation(live_hub, repo):
    seed_workspace(repo, room_messages=0)
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1024, 'height': 500})
        try:
            login_page(page, live_hub)
            assert page.locator('[data-ws-sidebar]').is_visible()
            assert not page.locator('[data-ws-sidebar-open]').is_visible()
        finally:
            browser.close()


@pytest.mark.parametrize('width,height', [(320, 640), (932, 430)])
def test_mobile_management_pages_fit_and_preserve_navigation(live_hub, repo, width, height):
    data = seed_workspace(repo, room_messages=0)
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': width, 'height': height}, is_mobile=True, has_touch=True)
        try:
            login_page(page, live_hub)
            paths = {
                'agent': f'/projects/workspace-team/agents/{data["controller"]["id"]}',
                'task': f'/projects/workspace-team/tasks/{data["tasks"][0]["id"]}',
                'archive': '/projects/workspace-team?view=archive',
            }
            for name, path in paths.items():
                page.goto(live_hub + path)
                page.wait_for_load_state('domcontentloaded')
                evidence(page, f'management-{name}-{width}')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), name
                if name == 'archive':
                    assert page.locator('.workbench-topbar').evaluate('n=>n.scrollHeight<=n.clientHeight+1')
                    page.locator('[data-sidebar-open]').click()
                    sidebar = page.locator('[data-project-sidebar]')
                    playwright.expect(sidebar).to_have_attribute('aria-hidden', 'false')
                    page.wait_for_function('document.querySelector("[data-project-sidebar]").getBoundingClientRect().x >= 0')
                    sidebar.locator('[data-sidebar-close]').click()
                    assert page.locator('.archive-workspace-return').get_attribute('href').endswith('/projects/workspace-team')
                if name != 'archive':
                    assert page.locator('.brand-logo').is_visible()
                    assert page.locator('.back-link,.agent-back-link').is_visible()
                    for field in page.locator('input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]),textarea,select').all():
                        if field.is_visible():
                            assert float(field.evaluate('n=>parseFloat(getComputedStyle(n).fontSize)')) >= 16, (name, field.get_attribute('name'))
                    page.locator('form button[type="submit"]').last.scroll_into_view_if_needed()
                    assert page.locator('form button[type="submit"]').last.is_visible()
        finally:
            browser.close()
