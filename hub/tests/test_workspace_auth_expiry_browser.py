"""Real-session expiry and recovery must preserve unsent text and browser Files."""
import hashlib

import pytest

from test_mobile_composer_browser import live_hub  # noqa: F401
from test_workspace_attachments_browser import DOCUMENT,PNG,request_key
from test_workspace_shell_browser import login_page
from workspace_fixture import seed_workspace

playwright=pytest.importorskip('playwright.sync_api')

OBSERVE_LIFECYCLE='''
    window.__sessionTest = {streams:[],delays:[],documentId:Math.random()};
    const nativeTimeout = window.setTimeout.bind(window);
    window.setTimeout = (callback,delay,...args) => {
        if(delay===30000) window.__sessionTest.delays.push(delay);
        // Preserve the production timer choice, shorten only its wall-clock wait.
        return nativeTimeout(callback,delay===30000?1000:delay,...args);
    };
    const NativeEventSource = window.EventSource;
    window.EventSource = class extends NativeEventSource {
        constructor(...args) {
            super(...args);window.__sessionTest.streams.push(this);this.testClosed=false;
        }
        close() {this.testClosed=true;super.close();}
    };
'''


@pytest.mark.parametrize('expiry_response',['401','redirect'])
def test_expired_login_keeps_attachments_and_recovers_stream_without_reload(live_hub,repo,expiry_response):
    data=seed_workspace(repo,room_messages=2)
    with playwright.sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        context=browser.new_context(viewport={'width':1440,'height':960})
        page=context.new_page();page.add_init_script(OBSERVE_LIFECYCLE)
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        expired={'value':False};redirects=[]
        room_url=f"{live_hub}/projects/workspace-team/conversations/{data['conversation_id']}"
        def workspace_response(route):
            if expired['value'] and expiry_response=='redirect':
                redirects.append(route.request.url)
                route.fulfill(status=302,headers={'Location':live_hub+'/login'},body='')
            else:route.continue_()
        page.route(room_url,workspace_response)
        try:
            login_page(page,live_hub)
            page.goto(room_url)
            page.wait_for_function("document.querySelector('[data-ws-shell]').dataset.wsReady==='true'")
            page.wait_for_function('window.__sessionTest.streams.some(stream=>stream.readyState===1)')
            description='重新登录后继续发送这份草稿和两个原始附件。'
            page.locator('[data-ws-input]').fill(description)
            page.locator('[data-ws-attachment-input]').set_input_files([
                {'name':'保留图片.png','mimeType':'image/png','buffer':PNG},
                {'name':'保留说明.txt','mimeType':'text/plain','buffer':DOCUMENT},
            ])
            playwright.expect(page.locator('[data-ws-attachment]')).to_have_count(2)
            original_key=request_key(page)
            original_document=page.evaluate('window.__sessionTest.documentId')
            session=next(cookie['value'] for cookie in context.cookies() if cookie['name']=='codex_hub_session')
            expired['value']=True;repo.delete_session(session)
            playwright.expect(page.locator('[data-ws-sync]')).to_contain_text('登录已过期',timeout=8000)
            link=page.locator('[data-ws-sync] a')
            playwright.expect(link).to_have_attribute('href','/login')
            playwright.expect(link).to_have_attribute('target','_blank')
            page.wait_for_function('window.__sessionTest.streams.every(stream=>stream.testClosed)')
            count=page.evaluate('window.__sessionTest.streams.length')
            # A retry still checks authentication at the slow production 30s
            # interval, and must not start a fresh SSE while the session is gone.
            page.wait_for_function('window.__sessionTest.delays.length>=2',timeout=6000)
            assert page.evaluate('window.__sessionTest.streams.length')==count
            assert page.locator('[data-ws-input]').input_value()==description
            assert request_key(page)==original_key
            playwright.expect(page.locator('[data-ws-attachment]')).to_have_count(2)
            if expiry_response=='redirect':assert redirects

            login_tab=context.new_page()
            login_page(login_tab,live_hub)
            expired['value']=False
            page.bring_to_front()
            playwright.expect(page.locator('[data-ws-sync]')).to_have_text('已同步',timeout=8000)
            page.wait_for_function('count => window.__sessionTest.streams.length>count && window.__sessionTest.streams.at(-1).readyState===1',arg=count,timeout=6000)
            assert page.evaluate('window.__sessionTest.documentId')==original_document
            assert page.locator('[data-ws-input]').input_value()==description
            assert request_key(page)==original_key
            playwright.expect(page.locator('[data-ws-attachment]')).to_have_count(2)
            # Successful upload verifies the File objects, not only the chips,
            # survived the session replacement and CSRF rotation in another tab.
            page.locator('[data-ws-send]').click()
            page.wait_for_function("document.querySelector('[data-ws-input]').value===''")
            created=[task for task in repo.list_tasks(data['project']['id']) if task['description']==description]
            assert len(created)==1
            attachments=repo.list_task_attachments(data['project']['id'],created[0]['id'])
            assert {item['sha256'] for item in attachments}=={hashlib.sha256(PNG).hexdigest(),hashlib.sha256(DOCUMENT).hexdigest()}
            assert not errors
        finally:browser.close()
