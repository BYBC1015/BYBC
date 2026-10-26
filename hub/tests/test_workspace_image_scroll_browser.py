"""Delayed real images must respect following the latest result or reading history."""
import hashlib
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from test_mobile_composer_browser import live_hub  # noqa: F401
from test_workspace_shell_browser import login_page, assert_composer_visible
from workspace_fixture import seed_workspace

playwright = pytest.importorskip('playwright.sync_api')
OUTPUT = Path(__file__).resolve().parents[2] / 'output/hub-workspace-scroll-20261027'


def add_image(repo, settings, data, name):
    buffer = BytesIO()
    Image.new('RGB', (640, 360), '#78968c').save(buffer, format='PNG')
    content = buffer.getvalue()
    relative = f"{data['project']['slug']}/{name}.png"
    target = settings.files_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    entry, files = repo.create_entry_with_attachments(project_id=data['project']['id'],
        task_id=data['tasks'][2]['id'], source_label='审查员', kind='message',
        body_markdown=f'已上传本轮界面图：{name}', author_device_id=data['connection']['id'], attachments=[{
            'original_filename':f'{name}.png', 'stored_relpath':relative, 'content_type':'image/png',
            'byte_size':len(content), 'sha256':hashlib.sha256(content).hexdigest()}])
    return entry, files[0]


def add_text(repo, data, text):
    return repo.create_entry(data['project']['id'], source_label='审查员', kind='message',
        body_markdown=text, task_id=data['tasks'][2]['id'], author_device_id=data['connection']['id'])


def render_frames(page):
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')


def bottom_distance(page):
    return page.locator('[data-ws-scroll]').evaluate('el => el.scrollHeight - el.clientHeight - el.scrollTop')


def entry_offset(page, entry_id):
    return page.locator(f'[data-ws-entry="{entry_id}"]').evaluate(
        'el => el.getBoundingClientRect().top - document.querySelector("[data-ws-scroll]").getBoundingClientRect().top')


def assert_latest_visible(page, entry_id):
    render_frames(page)
    assert bottom_distance(page) <= 2
    assert page.locator(f'[data-ws-entry="{entry_id}"] [data-ws-message-body]').evaluate('''el => {
        const box=el.getBoundingClientRect(), view=document.querySelector('[data-ws-scroll]').getBoundingClientRect();
        return box.top>=view.top && box.bottom<=view.bottom;
    }''')


class DelayedImages:
    def __init__(self, page):
        self.page, self.pending, self.requests = page, [], []
        page.route('**/projects/workspace-team/attachments/*', self.hold)

    def hold(self, route):
        if route.request.resource_type == 'image':
            self.requests.append(route.request.url)
            self.pending.append(route)
        else:
            route.continue_()

    def release(self):
        assert self.pending, 'Expected a genuinely deferred image response'
        while self.pending:
            route = self.pending.pop(0)
            response = route.fetch()
            assert response.status == 200
            route.fulfill(response=response)


@pytest.mark.parametrize('reading_history', [False, True], ids=['follow-latest', 'keep-reading-anchor'])
def test_delayed_images_and_refresh_preserve_the_users_reading_mode(live_hub, repo, settings, reading_history):
    data = seed_workspace(repo, room_messages=12)
    long_title = '核对网页端图片与文字交付，保留完整验收依据并检查协作结果。' * 7
    repo.connection.execute('UPDATE tasks SET title=? WHERE id=?', (long_title, data['tasks'][2]['id']))
    repo.connection.commit()
    initial_image, first_file = add_image(repo, settings, data, 'initial-640x360')
    reading_entries = [add_text(repo, data, f'阅读位置 {number}\n\n保留正在阅读的工作记录和上下文。') for number in range(6)]
    last = add_text(repo, data, '最终交付已经完成：请查看本条结果。')
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':1440,'height':960})
        page.set_default_timeout(10000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            login_page(page, live_hub)
            delayed = DelayedImages(page)
            room_url = f"{live_hub}/projects/workspace-team/conversations/{data['conversation_id']}"
            page.goto(room_url, wait_until='domcontentloaded')
            first_image = page.locator(f'[data-ws-entry="{initial_image["id"]}"] img')
            render_frames(page)
            assert first_image.evaluate('el => el.naturalWidth') == 0
            assert delayed.pending
            assert_latest_visible(page, last['id'])

            anchor_id = reading_entries[0]['id']
            if reading_history:
                page.locator(f'[data-ws-entry="{anchor_id}"]').evaluate('''el => {
                    const view=document.querySelector('[data-ws-scroll]');
                    view.scrollTop += el.getBoundingClientRect().top-view.getBoundingClientRect().top+10;
                }''')
                render_frames(page)
                assert bottom_distance(page) > 200
                offset = entry_offset(page, anchor_id)
            delayed.release()
            page.wait_for_function('el => el.naturalWidth === 640 && el.naturalHeight === 360', arg=first_image.element_handle())
            render_frames(page)
            assert first_image.bounding_box()['height'] > 200
            if reading_history:
                assert abs(entry_offset(page, anchor_id)-offset) <= 2
                assert bottom_distance(page) > 200
            else:
                assert_latest_visible(page, last['id'])

            # The real ETag poll replaces feed contents. Its newly introduced
            # image must also receive the same late-load handling.
            with page.expect_response(lambda response: response.url == room_url and
                    response.request.resource_type == 'fetch' and response.status == 200, timeout=12000):
                new_image, second_file = add_image(repo, settings, data, 'refreshed-640x360')
                final = add_text(repo, data, '刷新后的最后结果：图片读取与验收均已完成。')
            playwright.expect(page.locator(f'[data-ws-entry="{final["id"]}"]')).to_have_count(1)
            render_frames(page)
            new_image_node = page.locator(f'[data-ws-entry="{new_image["id"]}"] img')
            assert new_image_node.evaluate('el => el.naturalWidth') == 0
            assert any(f"/attachments/{second_file['id']}" in url for url in delayed.requests)
            if reading_history:
                assert abs(entry_offset(page, anchor_id)-offset) <= 2
                assert bottom_distance(page) > 200
            else:
                assert_latest_visible(page, final['id'])
            delayed.release()
            page.wait_for_function('el => el.naturalWidth === 640', arg=new_image_node.element_handle())
            render_frames(page)
            if reading_history:
                assert abs(entry_offset(page, anchor_id)-offset) <= 2
                assert bottom_distance(page) > 200
            else:
                assert_latest_visible(page, final['id'])
                footer = page.locator(f'[data-ws-entry="{final["id"]}"] .ws-message-source')
                assert footer.inner_text().strip() == f"查看任务 #{data['tasks'][2]['id']} ↗"
                assert footer.get_attribute('title') == long_title
                footer.click()
                title = page.locator(f'[data-ws-task-panel="{data["tasks"][2]["id"]}"] .ws-inspector-title')
                assert title.get_attribute('title') == long_title
                assert title.evaluate('el => el.getBoundingClientRect().height <= parseFloat(getComputedStyle(el).lineHeight)*3+1')
                full = page.locator(f'[data-ws-task-panel="{data["tasks"][2]["id"]}"] .ws-full-link')
                assert full.get_attribute('href').endswith(f"/tasks/{data['tasks'][2]['id']}")
            assert_composer_visible(page, 960)
            assert not errors
            OUTPUT.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(OUTPUT / ('reading-anchor.png' if reading_history else 'latest-result.png')))
        except Exception:
            OUTPUT.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(OUTPUT / ('failure-reading.png' if reading_history else 'failure-latest.png')))
            raise
        finally:
            browser.close()
