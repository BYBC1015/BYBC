"""Reply presentation on an isolated server and headless Chromium only."""
from pathlib import Path
import json
import time

import httpx
import pytest

from test_mobile_composer_browser import live_hub  # noqa: F401
from test_workspace_shell_browser import login_page
from test_desktop_progress_browser import running_task, publish, open_room
from workspace_fixture import seed_workspace

playwright = pytest.importorskip('playwright.sync_api')
OUTPUT = Path(__file__).resolve().parents[2] / 'output/hub-product-maintenance-20261030/ui/reply-states'
ANIMATION_CAPTURE = '''
window.__replyAnimations=[];
document.addEventListener('animationstart',event=>{
  if(event.animationName.startsWith('ws-reply-'))window.__replyAnimations.push(event.animationName);
});
window.pywebview={api:{list_projects:async()=>[],open_project:async()=>({opened:true})}};
'''


def screenshot(page, name):
    OUTPUT.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(OUTPUT/name))


def prepare_scene(repo,data,task):
    # Replace the shared fixture's deliberate HTML-injection sample with a
    # legible demonstration for screenshots. Security remains covered elsewhere.
    with repo.connection:
        repo.connection.execute('UPDATE entries SET body_markdown=? WHERE id=?',
            ('准备好了。你可以随时补充要求，我会把工作进展留在当前对话里。',data['last_entry_id']))
        repo.connection.execute('UPDATE tasks SET title=? WHERE id=?',('优化 Hub 回复体验',task['id']))
    repo.create_entry(data['project']['id'],'你','message',
        '优化一下本地 Hub 的回复体验：状态清楚一点，界面更舒服。',
        author_user_id=repo.get_user_by_username('owner')['id'],task_id=task['id'])


def test_real_reply_states_offline_retention_and_final_delivery(live_hub,repo):
    data=seed_workspace(repo,room_messages=0)
    task,run,headers=running_task(repo,data)
    prepare_scene(repo,data,task)
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        context=browser.new_context(viewport={'width':1440,'height':1000})
        page=context.new_page()
        page.add_init_script(ANIMATION_CAPTURE)
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        try:
            login_page(page,live_hub)
            open_room(page,live_hub,data['conversation_id'])
            card=page.locator(f'[data-ws-progress="{task["id"]}"]')
            playwright.expect(card.locator('[data-ws-progress-label-text]')).to_have_text('等待公开进展')
            card.locator('[data-ws-task]').click()
            page.locator('[data-ws-input]').focus()
            playwright.expect(card.locator('[data-ws-progress-body]')).to_be_hidden()
            assert not card.locator('details').get_attribute('open')
            screenshot(page,'reply-01-waiting-desktop.png')
            publish(live_hub,task,run,headers,1,'status','正在执行工具 · 核对项目素材')
            playwright.expect(card.locator('.ws-progress-status')).to_have_text('最近报告：正在执行工具 · 核对项目素材',timeout=8000)
            playwright.expect(card.locator('[data-ws-progress-label-text]')).to_have_text('执行中')
            screenshot(page,'reply-02-tool-desktop.png')
            text='我已经对齐了这次的方向。\n\n**先让交互更顺，再打磨每一处细节。**\n\n- 回复到达时立即显示\n- 工具进展与结果各自清楚\n- 你可以同时补充新的想法'
            publish(live_hub,task,run,headers,2,'assistant',text)
            playwright.expect(card.locator('[data-ws-progress-label-text]')).to_have_text('正在回复',timeout=8000)
            playwright.expect(card.locator('[data-ws-progress-body]')).to_contain_text('你可以同时补充新的想法')
            family=card.locator('[data-ws-progress-body]').evaluate('node=>getComputedStyle(node).fontFamily')
            assert 'Microsoft YaHei UI' in family and family.endswith('sans-serif')
            logo=page.locator('.ws-brand-icon img')
            assert logo.get_attribute('src').endswith('/static/assets/codex-owl-logo-lite-20260708.png')
            assert logo.evaluate('node=>node.complete && node.naturalWidth>0')
            session=context.new_cdp_session(page)
            session.send('DOM.enable');session.send('CSS.enable')
            document=session.send('DOM.getDocument')
            node_id=session.send('DOM.querySelector',{'nodeId':document['root']['nodeId'],
                'selector':'[data-ws-progress-body] .codex-markdown p'})['nodeId']
            OUTPUT.mkdir(parents=True,exist_ok=True)
            (OUTPUT/'reply-font-proof.json').write_text(json.dumps({'computed_family':family,
                'platform_fonts':session.send('CSS.getPlatformFontsForNode',{'nodeId':node_id})},ensure_ascii=False,indent=2),encoding='utf-8')
            page.locator('[data-ws-input]').fill('这份草稿在离线、恢复和收到结果后都应保留。')
            screenshot(page,'reply-03-response-desktop.png')
            context.set_offline(True)
            playwright.expect(card.locator('[data-ws-progress-label-text]')).to_have_text('等待连接',timeout=5000)
            playwright.expect(card.locator('[data-reply-connection-note]')).to_be_visible()
            playwright.expect(card.locator('[data-ws-progress-body]')).to_contain_text('你可以同时补充新的想法')
            assert not card.evaluate("node=>node.getAnimations({subtree:true}).some(a=>a.playState==='running')")
            screenshot(page,'reply-04-offline-desktop.png')
            context.set_offline(False)
            playwright.expect(card.locator('[data-ws-progress-label-text]')).to_have_text('正在回复',timeout=12000)
            repo.connection.execute('UPDATE hub_task_executions SET lease_until=? WHERE task_id=?',(time.time()-1,task['id']))
            repo.connection.commit()
            playwright.expect(card.locator('[data-ws-progress-label-text]')).to_have_text('执行状态待核对',timeout=8000)
            playwright.expect(card.locator('[data-ws-progress-warning]')).to_be_visible()
            assert card.locator('[data-ws-progress-details]').get_attribute('open') is None
            screenshot(page,'reply-05-unconfirmed-desktop.png')
            final='这轮回复与状态展示已完成，结果已经可以查看。'
            response=httpx.post(f'{live_hub}/api/projects/workspace-team/tasks/{task["id"]}/execution/complete',
                headers=headers,json={'generation':run['generation'],'run_key':run['run_key'],
                    'status':'review','summary':'等待实际验收','body':final,
                    'reflection':{'summary':'隔离浏览器检查','worked':'状态验证','failed':'','next_time':'保持回归'}},timeout=5)
            assert response.status_code==200,response.text
            result=page.locator('[data-ws-entry]').filter(has_text=final)
            playwright.expect(result).to_have_count(1,timeout=8000)
            playwright.expect(card).to_have_count(0)
            playwright.expect(result.locator('.ws-reply-delivered')).to_have_text('已回复')
            assert '已验收' not in result.inner_text()
            assert page.locator('[data-ws-input]').input_value()=='这份草稿在离线、恢复和收到结果后都应保留。'
            screenshot(page,'reply-06-delivered-desktop.png')
            assert not errors
        finally:
            browser.close()


def test_timing_refresh_keeps_details_and_does_not_replay_reply_motion(live_hub,repo,monkeypatch):
    import codex_hub.execution_progress as progress
    monkeypatch.setattr(progress,'ACTIVITY_CLOCK_SECONDS',1)
    data=seed_workspace(repo,room_messages=0)
    task,run,headers=running_task(repo,data)
    prepare_scene(repo,data,task)
    publish(live_hub,task,run,headers,1,'assistant','保留这段公开回复，不要因计时变化反复淡入。')
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1440,'height':960})
        page.add_init_script(ANIMATION_CAPTURE)
        try:
            login_page(page,live_hub)
            open_room(page,live_hub,data['conversation_id'])
            card=page.locator(f'[data-ws-progress="{task["id"]}"]')
            card.locator('summary').click()
            playwright.expect(card.locator('[data-ws-progress-timing]')).to_be_visible()
            page.locator('[data-ws-input]').fill('保持编辑')
            before=page.evaluate('window.__replyAnimations.length')
            version=card.get_attribute('data-ws-entry-version')
            body_version=card.get_attribute('data-ws-progress-body-version')
            page.wait_for_function('([id,v])=>document.querySelector(`[data-ws-progress="${id}"]`)?.dataset.wsEntryVersion!==v',arg=[task['id'],version],timeout=8000)
            assert card.locator('details').get_attribute('open') is not None
            assert card.get_attribute('data-ws-progress-body-version')==body_version
            assert page.evaluate('window.__replyAnimations.length')==before
            assert page.locator('[data-ws-input]').input_value()=='保持编辑'
            publish(live_hub,task,run,headers,2,'assistant','保留这段公开回复，不要因计时变化反复淡入。\n\n现在收到真实的新内容。')
            playwright.expect(card).to_contain_text('现在收到真实的新内容',timeout=8000)
            page.wait_for_function('n=>window.__replyAnimations.length>n',arg=before,timeout=3000)
            assert not card.evaluate('node=>node.getAnimations({subtree:true}).some(a=>a.effect.getTiming().iterations===Infinity)')
        finally:
            browser.close()


def test_reduced_motion_mobile_has_clear_states_without_any_reply_animation(live_hub,repo):
    data=seed_workspace(repo,room_messages=0)
    task,run,headers=running_task(repo,data)
    prepare_scene(repo,data,task)
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':390,'height':844},reduced_motion='reduce')
        page.add_init_script(ANIMATION_CAPTURE)
        try:
            login_page(page,live_hub)
            open_room(page,live_hub,data['conversation_id'])
            publish(live_hub,task,run,headers,1,'assistant','回复已到达。手机上的内容也保持完整，减少动态效果不会影响消息更新。')
            card=page.locator(f'[data-ws-progress="{task["id"]}"]')
            playwright.expect(card).to_contain_text('减少动态效果不会影响消息更新',timeout=8000)
            assert page.evaluate('window.__replyAnimations.length')==0
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
            assert card.locator('.ws-reply-card').evaluate('node=>getComputedStyle(node).borderRadius')=='18px'
            assert page.locator('[data-ws-input]').bounding_box()['y']<844
            screenshot(page,'reply-07-mobile-reduced-motion.png')
        finally:
            browser.close()
