"""Product UI checks use an isolated Hub; keyboard geometry is explicitly simulated."""
from pathlib import Path
import json

import pytest

from test_mobile_composer_browser import live_hub  # noqa: F401
from test_workspace_shell_browser import login_page
from test_desktop_progress_browser import running_task, publish, open_room
from test_reply_activity_browser import prepare_scene
from workspace_fixture import seed_workspace

playwright = pytest.importorskip('playwright.sync_api')
OUTPUT = Path(__file__).resolve().parents[2] / 'output/hub-product-maintenance-20261030/ui'

VISUAL_VIEWPORT = '''
const fakeViewport=new EventTarget();
Object.assign(fakeViewport,{height:innerHeight,width:innerWidth,scale:1,offsetTop:0,offsetLeft:0});
Object.defineProperty(window,'visualViewport',{get:()=>fakeViewport});
window.__setViewport=values=>{Object.assign(fakeViewport,values);fakeViewport.dispatchEvent(new Event('resize'));};
window.__animations=[];
document.addEventListener('animationstart',event=>window.__animations.push(event.animationName));
'''


def save_json(name, value):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def screenshot(page, name):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUTPUT / name))


def inspect_fonts(context, page, selectors):
    session = context.new_cdp_session(page)
    session.send('DOM.enable')
    session.send('CSS.enable')
    document = session.send('DOM.getDocument', {'depth': -1, 'pierce': True})
    result = {}
    for label, selector in selectors.items():
        node_id = session.send('DOM.querySelector', {'nodeId': document['root']['nodeId'], 'selector': selector})['nodeId']
        style = page.locator(selector).first.evaluate('node=>({family:getComputedStyle(node).fontFamily,weight:getComputedStyle(node).fontWeight,size:getComputedStyle(node).fontSize,lineHeight:getComputedStyle(node).lineHeight})')
        result[label] = {'computed': style, **session.send('CSS.getPlatformFontsForNode', {'nodeId': node_id})}
        if label == 'input' and not result[label]['fonts']:
            # Chromium paints textarea text inside its user-agent shadow tree.
            described = session.send('DOM.describeNode', {'nodeId': node_id, 'depth': -1, 'pierce': True})['node']
            queue = list(described.get('shadowRoots', []))
            control_fonts = []
            while queue:
                child = queue.pop()
                queue.extend(child.get('children', []))
                queue.extend(child.get('shadowRoots', []))
                if child.get('nodeType') == 1 and child.get('nodeId'):
                    control_fonts.extend(session.send('CSS.getPlatformFontsForNode', {'nodeId': child['nodeId']})['fonts'])
            result[label]['control_shadow_fonts'] = control_fonts
    return result


def test_brand_and_actual_cjk_typography_keep_hierarchy(live_hub, repo):
    data = seed_workspace(repo, room_messages=0)
    task, run, headers = running_task(repo, data)
    prepare_scene(repo, data, task)
    publish(live_hub, task, run, headers, 1, 'assistant', '## 让协作更清楚\n\n每条回复都有清晰的层次，正文保持舒适的黑体字形。\n\n**重点信息适度加粗，普通内容保持正常字重。**\n\n```python\nprint("hello")\n```')
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        page = context.new_page()
        try:
            login_page(page, live_hub)
            open_room(page, live_hub, data['conversation_id'])
            page.locator('[data-ws-input]').fill('请继续完善这次的协作体验。')
            logo = page.locator('.ws-brand-icon img')
            playwright.expect(logo).to_be_visible()
            assert logo.get_attribute('src').split('?', 1)[0].endswith('codex-owl-logo-lite-20260708.png')
            assert logo.evaluate('node=>node.complete&&node.naturalWidth===192')
            assert page.locator('.ws-brand-icon').inner_text() == ''
            assert page.locator('.ws-brand-icon').get_attribute('aria-label') == 'Hub 项目首页'
            fonts = inspect_fonts(context, page, {'body': '[data-ws-progress-body] p', 'heading': '[data-ws-progress-body] h2', 'input': '[data-ws-input]', 'title': '.ws-chat-title h1', 'code': '[data-ws-progress-body] pre code'})
            save_json('actual-platform-fonts.json', fonts)
            for label in ['body', 'heading', 'title']:
                assert fonts[label]['fonts'], fonts[label]
                names = [font['familyName'] for font in fonts[label]['fonts'] if font['glyphCount']]
                assert any(any(sans in name for sans in ['Noto Sans', 'YaHei', 'PingFang', 'Heiti', 'Hei']) for name in names), names
                assert not any('Serif' in name or name in ['SimSun', 'NSimSun', 'Times New Roman'] for name in names), names
            assert fonts['body']['computed']['weight'] == '400'
            assert fonts['heading']['computed']['weight'] == '600'
            assert fonts['body']['computed']['size'] == fonts['input']['computed']['size'] == '16px'
            input_fonts = fonts['input']['fonts'] or fonts['input'].get('control_shadow_fonts', [])
            assert input_fonts and any('Noto Sans' in font['familyName'] or 'YaHei' in font['familyName'] or 'PingFang' in font['familyName'] for font in input_fonts), input_fonts
            assert 'monospace' in fonts['code']['computed']['family']
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
            screenshot(page, 'desktop-black-sans-owl.png')
        finally:
            browser.close()


@pytest.mark.parametrize('width,height', [(390, 844), (360, 780)])
def test_mobile_keyboard_preserves_reading_draft_and_visible_send(live_hub, repo, width, height):
    data = seed_workspace(repo, room_messages=45)
    task, run, headers = running_task(repo, data)
    prepare_scene(repo, data, task)
    initial_reply = '## 手机上的协作\n\n长消息不会挤掉发送按钮。\n\n- 回复持续更新\n- 附件和草稿保持完整\n- 工具进展清晰可见'
    publish(live_hub, task, run, headers, 1, 'assistant', initial_reply)
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': width, 'height': height}, is_mobile=True, has_touch=True, reduced_motion='reduce')
        page = context.new_page()
        page.add_init_script(VISUAL_VIEWPORT)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            login_page(page, live_hub)
            open_room(page, live_hub, data['conversation_id'])
            draft = '补充手机端的验收要求，键盘弹出后仍然可以发送。'
            page.locator('[data-ws-input]').fill(draft)
            assert page.locator('[data-ws-input]').evaluate('el=>getComputedStyle(el).fontSize') == '16px'
            for selector in ['[data-ws-sidebar-open]', '[data-ws-inspector-open]', '[data-ws-send]', '[data-ws-attach]']:
                bounds = page.locator(selector).bounding_box()
                assert bounds and bounds['height'] >= 44 and bounds['width'] >= 44, (selector, bounds)
            assert page.locator('.ws-chat-title h1').bounding_box()['width'] >= 100
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
            screenshot(page, f'mobile-{width}-normal.png')
            page.locator('[data-ws-sidebar-open]').click()
            playwright.expect(page.locator('.ws-brand-icon img')).to_be_visible()
            screenshot(page, f'mobile-{width}-navigation-owl.png')
            page.locator('[data-ws-sidebar-close]').click()
            page.locator('[data-ws-input]').focus()
            # Safari/WKWebView can keep its layout viewport while only this area shrinks.
            visual_height = 390 if width == 390 else 360
            page.evaluate('height=>window.__setViewport({height,offsetTop:14})', visual_height)
            playwright.expect(page.locator('[data-ws-shell]')).to_have_attribute('data-ws-keyboard-open', 'true')
            send = page.locator('[data-ws-send]').bounding_box()
            box = page.locator('[data-ws-input]').bounding_box()
            assert send['y'] >= 14 and send['y'] + send['height'] <= visual_height + 14
            assert box['y'] >= 14 and box['y'] + box['height'] <= send['y']
            assert page.locator('[data-ws-scroll]').bounding_box()['height'] >= 65
            assert page.locator('[data-ws-input]').input_value() == draft
            screenshot(page, f'mobile-{width}-keyboard-geometry.png')
            page.locator('[data-ws-attachment-input]').set_input_files([
                {'name': '手机需求说明.txt', 'mimeType': 'text/plain', 'buffer': '手机键盘与附件发送验收'.encode()},
                {'name': '第二份参考.txt', 'mimeType': 'text/plain', 'buffer': b'another reference'},
            ])
            playwright.expect(page.locator('[data-ws-attachments]')).to_be_visible()
            send_with_files = page.locator('[data-ws-send]').bounding_box()
            save_json(f'mobile-{width}-attachment-layout.json', page.evaluate('''()=>({keyboard:document.querySelector('[data-ws-shell]').dataset.wsKeyboardOpen,active:document.activeElement?.outerHTML.slice(0,180),layout:Array.from(document.querySelectorAll('.ws-center,.ws-messages,.ws-composer-region,.ws-composer,.ws-recipient-status,.ws-composer-context,.ws-composer textarea,.ws-attachments,.ws-composer-bottom')).map(node=>({class:node.className,rect:node.getBoundingClientRect().toJSON(),maxHeight:getComputedStyle(node).maxHeight,minHeight:getComputedStyle(node).minHeight}))})'''))
            assert send_with_files['y'] + send_with_files['height'] <= visual_height + 14, send_with_files
            assert page.locator('[data-ws-scroll]').bounding_box()['height'] >= 50
            screenshot(page, f'mobile-{width}-keyboard-attachments.png')
            # Tapping a control can blur the input before the keyboard finishes
            # hiding; keep the composer in view throughout that transition.
            page.locator('[data-ws-input]').evaluate('node=>node.blur()')
            playwright.expect(page.locator('[data-ws-shell]')).to_have_attribute('data-ws-compact-viewport', 'true')
            assert page.locator('[data-ws-send]').bounding_box()['y'] + 44 <= visual_height + 14
            # A real report still arrives and reduced-motion stays quiet while keyboard is up.
            publish(live_hub, task, run, headers, 2, 'assistant', initial_reply + '\n\n键盘弹出期间的新回复已经到达。')
            playwright.expect(page.locator('[data-ws-progress-body]')).to_contain_text('新回复已经到达', timeout=8000)
            assert page.evaluate('window.__animations.filter(name=>name.startsWith("ws-reply-")).length') == 0
            assert page.locator('[data-ws-input]').input_value() == draft
            # Pinch zoom must not trigger a competing layout resize.
            preserved = page.locator('[data-ws-shell]').evaluate('node=>node.style.getPropertyValue("--ws-viewport-height")')
            page.evaluate('window.__setViewport({height:220,scale:2})')
            page.wait_for_timeout(60)
            assert page.locator('[data-ws-shell]').evaluate('node=>node.style.getPropertyValue("--ws-viewport-height")') == preserved
            page.evaluate('height=>window.__setViewport({height,offsetTop:0,scale:1})', height)
            playwright.expect(page.locator('[data-ws-shell]')).to_have_attribute('data-ws-keyboard-open', 'false')
            assert page.locator('[data-ws-input]').input_value() == draft
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
            save_json(f'mobile-{width}-geometry.json', {'viewport': {'width': width, 'height': height}, 'simulated_visual_keyboard_height': visual_height, 'keyboard_top': 14, 'send_bounds': send, 'input_bounds': box, 'real_device_test': False, 'errors': errors})
            assert errors == []
        finally:
            browser.close()


def test_mobile_agent_creation_keeps_submit_above_visual_keyboard(live_hub, repo):
    data = seed_workspace(repo, room_messages=0)
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
        page.add_init_script(VISUAL_VIEWPORT)
        try:
            login_page(page, live_hub)
            page.locator('[data-ws-input]').fill('保留主对话草稿')
            page.locator('[data-ws-sidebar-open]').click()
            page.locator('.ws-agent-add[data-ws-create-agent]').click()
            form = page.locator('[data-agent-create]')
            playwright.expect(form).to_be_visible()
            form.locator('[name="name"]').fill('移动协作测试员')
            form.locator('[name="job"]').fill('检查手机编辑体验')
            form.locator('[name="instructions"]').fill('保留草稿，按职责处理工作。')
            page.evaluate('window.__setViewport({height:390,offsetTop:14})')
            page.wait_for_function('document.querySelector("[data-ws-shell]").dataset.wsCompactViewport==="true"')
            dialog = page.locator('[data-agent-workspace]')
            submit = form.locator('button[type="submit"]')
            bounds = submit.bounding_box()
            assert bounds and bounds['y'] >= 14 and bounds['y'] + bounds['height'] <= 404, bounds
            assert dialog.bounding_box()['height'] <= 390
            assert form.locator('[name="instructions"]').evaluate('node=>getComputedStyle(node).fontSize') == '16px'
            screenshot(page, 'mobile-agent-form-keyboard.png')
            submit.click()
            playwright.expect(form).to_be_hidden()
            assert any(agent['name'] == '移动协作测试员' for agent in data['store'].list(data['project']['id']))
            # Successful creation opens the new profile. Returning to the room
            # restores its existing draft rather than treating this as a send.
            page.goto(live_hub + '/projects/workspace-team')
            playwright.expect(page.locator('[data-ws-shell]')).to_have_attribute('data-ws-ready', 'true')
            playwright.expect(page.locator('[data-ws-input]')).to_have_value('保留主对话草稿')
        finally:
            browser.close()
