"""Network changes must refresh UI without replaying work or discarding drafts."""
from pathlib import Path
import pytest

playwright = pytest.importorskip('playwright.sync_api')
ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'android/app/src/main/assets'
ADDRESS = 'https://hub.invalid/hub/projects/demo/conversations/7'


def document(release='', user='5'):
    return f'''<!doctype html><html><head><meta charset="UTF-8"></head><body><main data-ws-shell data-user="{user}"
      data-project="demo" data-conversation="7" data-ws-release="{release}">
      <article>保持可读的聊天记录</article><form data-ws-dispatch><input type="hidden" name="_csrf" value="fresh-test-only">
      <textarea data-ws-input></textarea><input type="file"><div data-ws-attachments></div></form>
      </main></body></html>'''


@pytest.fixture
def native_page():
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':390,'height':844}, is_mobile=True, has_touch=True)
        state = {'initial':document(), 'fresh':document('2.9.6'), 'status':200, 'requests':[]}
        def reply(route):
            state['requests'].append((route.request.method, route.request.resource_type, route.request.url))
            initial = route.request.resource_type == 'document' and '_hub_native_ui=' not in route.request.url
            route.fulfill(status=200 if initial else state['status'], content_type='text/html', body=state['initial'] if initial else state['fresh'])
        page.route('**/*', reply)
        yield page, state
        browser.close()


def start(page):
    page.goto(ADDRESS)
    page.evaluate("window.__saved={draft:null,job:null};window.CodexWorkspaceStore={read:async()=>__saved};window.__online=0;window.addEventListener('online',()=>__online++)")


def inject(page):
    page.add_script_tag(path=str(ASSETS / 'android-recovery.js'))


def test_current_workspace_wakes_shared_refresh_once_per_network_burst(native_page):
    page, state = native_page
    state['initial'] = document('2.9.6')
    start(page); inject(page); inject(page)
    page.evaluate("for(let i=0;i<12;i++)CodexHubNativeRecovery.wake(true)")
    page.wait_for_function('__online===1')
    assert len(state['requests']) == 1  # Shared workspace owns the one refresh.
    assert page.url == ADDRESS


def test_old_workspace_idle_migrates_to_verified_current_release(native_page):
    page, state = native_page
    start(page); inject(page)
    page.wait_for_url('**?_hub_native_ui=2.9.6')
    assert page.locator('[data-ws-shell]').get_attribute('data-ws-release') == '2.9.6'
    assert all(method == 'GET' for method, _, _ in state['requests'])


@pytest.mark.parametrize('kind', ['text', 'selected-file', 'saved-file', 'queued', 'sending'])
def test_recovery_preserves_draft_files_and_inflight_work(native_page, kind):
    page, state = native_page
    start(page)
    if kind == 'text': page.locator('textarea').fill('用户正在写，不能刷新')
    elif kind == 'selected-file': page.locator('input[type=file]').set_input_files({'name':'brief.txt','mimeType':'text/plain','buffer':b'retain'})
    elif kind == 'saved-file': page.evaluate("__saved.draft={files:[{id:'local-B'}]}")
    elif kind == 'queued': page.evaluate("__saved.job={state:'uncertain',key:'original-request'}")
    else: page.locator('form').evaluate("form=>form.setAttribute('aria-busy','true')")
    inject(page)
    page.wait_for_selector('[data-native-ui-update]')
    page.locator('[data-native-ui-update] button').click()
    page.wait_for_timeout(650)
    assert page.url == ADDRESS
    assert page.locator('article').inner_text() == '保持可读的聊天记录'
    assert all(method == 'GET' for method, _, _ in state['requests'])
    if kind == 'text': assert page.locator('textarea').input_value() == '用户正在写，不能刷新'
    if kind == 'selected-file': assert page.locator('input[type=file]').evaluate('e=>e.files[0].name') == 'brief.txt'


@pytest.mark.parametrize('failure', ['different-account', 'different-room', 'login', 'offline'])
def test_unverified_page_cannot_replace_current_conversation(native_page, failure):
    page, state = native_page
    if failure == 'different-account': state['fresh'] = document('2.9.6', user='9')
    elif failure == 'different-room': state['fresh'] = document('2.9.6').replace('data-conversation="7"', 'data-conversation="8"')
    elif failure == 'login': state['fresh'] = '<form>登录</form>'
    else: state['status'] = 503
    start(page); inject(page)
    page.wait_for_timeout(750)
    assert page.url == ADDRESS
    assert page.locator('article').inner_text() == '保持可读的聊天记录'
    assert all(method == 'GET' for method, _, _ in state['requests'])


def test_current_workspace_cannot_accidentally_enable_legacy_adapter(native_page):
    page, state = native_page
    state['initial'] = document('2.9.6').replace('</main>', '<div class="workbench-shell workbench-grid workbench-topbar"><div class="control-pane workbench-sidebar" data-auto-refresh="chat"></div></div></main>')
    start(page)
    page.add_script_tag(path=str(ASSETS / 'android-workspace.js'))
    page.add_script_tag(path=str(ASSETS / 'android-app.js'))
    assert page.locator('body').evaluate('body=>body.classList.contains("android-chat-mode")') is False
    assert page.evaluate('typeof CodexHubAndroid.back') == 'function'


def test_native_lifecycle_keeps_storage_and_uses_fresh_recovery_get():
    source = (ROOT / 'android/app/src/main/java/xyz/baicang/codexhub/MainActivity.java').read_text(encoding='utf-8')
    lifecycle = source[source.index('private void handleNetworkStateChanged()'):source.index('private void showConnectionRecovery(')]
    assert lifecycle.index('notifyWorkspaceNetwork();') < lifecycle.index('mainFrameFailureCount <= 0')
    assert 'CodexHubNativeRecovery.wake' in lifecycle
    recovery = source[source.index('private void loadPrimaryUrl('):source.index('private void configureWindowInsets()')]
    assert 'setCacheMode(WebSettings.LOAD_NO_CACHE)' in recovery
    assert 'clearCache' not in recovery and 'removeAllCookies' not in source
    assert "if(document.querySelector('[data-ws-shell]'))" in source
