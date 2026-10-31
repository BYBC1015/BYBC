"""Native app restoration and records must never cross an identity or a room."""
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]
JAVA = ROOT / 'android/app/src/main/java/xyz/baicang/codexhub'


def test_native_identity_room_age_and_frame_policy(tmp_path):
    jdk = ROOT.parent / '.local-tools/jdk-17.0.19+10/bin'
    javac = shutil.which('javac') or str(jdk / 'javac.exe')
    java = shutil.which('java') or str(jdk / 'java.exe')
    harness = tmp_path / 'NativeRecordCheck.java'
    harness.write_text(r'''
package xyz.baicang.codexhub;
public class NativeRecordCheck {
  static void check(boolean valid) { if (!valid) throw new AssertionError(); }
  public static void main(String[] args) {
    String base="https://hub.invalid/hub/", room=base+"projects/demo/conversations/7";
    String token=HubLocalRecordPolicy.session("a=foo; codex_hub_session=private-token; codex_hub_csrf=x");
    check(token.length()==64 && !token.contains("private-token"));
    check(HubLocalRecordPolicy.session("a=foo").isEmpty());
    check(HubLocalRecordPolicy.session("codex_hub_session=").isEmpty());
    check(token.equals(HubLocalRecordPolicy.session("codex_hub_session=private-token")));
    check(HubLocalRecordPolicy.readable(base,room,room,token,token,1000,2000));
    check(!HubLocalRecordPolicy.readable(base,room,room,"",token,1000,2000));
    check(!HubLocalRecordPolicy.readable(base,room,room,"other-session",token,1000,2000));
    check(!HubLocalRecordPolicy.readable(base,room+"0",room,token,token,1000,2000));
    check(!HubLocalRecordPolicy.readable(base,room,room,token,token,2001,2000));
    check(!HubLocalRecordPolicy.readable(base,room,room,token,token,1000,1001+HubLocalRecordPolicy.MAX_AGE_MS));
    check(!HubLocalRecordPolicy.readable(base,"https://other.invalid/hub/projects/demo/conversations/7",room,token,token,1000,2000));
    check(HubLocalRecordPolicy.matchesWorkspace(base,room,"2","demo","7"));
    check(!HubLocalRecordPolicy.matchesWorkspace(base,room,"2","elsewhere","7"));
    check(!HubLocalRecordPolicy.matchesWorkspace(base,room,"2","demo","8"));
    check(!HubLocalRecordPolicy.matchesWorkspace(base,room,"","demo","7"));
    check(!HubLocalRecordPolicy.matchesWorkspace(base,room,"2","demo","new"));
    check(HubLocalRecordPolicy.matchesWorkspace(base,base+"projects/demo","2","demo","new"));
    String archive=base+"projects/demo?view=archive&tab=assets";
    check(!HubNavigationPolicy.workspace(base,archive));
    check(HubNavigationPolicy.recoverable(base,archive));
    check(HubNavigationPolicy.currentPage(base,archive).equals(archive));
    check(HubNavigationPolicy.currentPage(base,room+"?task=42#work-entry-4").equals(room+"?task=42#work-entry-4"));
    check(HubNavigationPolicy.trustedBridge(base,room,"https://hub.invalid",true,true,"nonce","nonce"));
    check(!HubNavigationPolicy.trustedBridge(base,room,"https://hub.invalid",false,true,"nonce","nonce"));
    check(!HubNavigationPolicy.trustedBridge(base,room,"https://foreign.invalid",true,true,"nonce","nonce"));
    check(!HubNavigationPolicy.trustedBridge(base,room,"https://hub.invalid:444",true,true,"nonce","nonce"));
    check(!HubNavigationPolicy.trustedBridge(base,room,"https://hub.invalid",true,false,"nonce","nonce"));
    check(!HubNavigationPolicy.trustedBridge(base,room,"https://hub.invalid",true,true,"new","old"));
    check(!HubNavigationPolicy.trustedBridge(base,base+"projects/demo/attachments/1/preview","https://hub.invalid",true,true,"nonce","nonce"));
    check(!HubNavigationPolicy.trustedBridge(base,archive,"https://hub.invalid",true,true,"nonce","nonce"));
    check(HubNavigationPolicy.currentRelease("2.11.0"));
    check(HubNavigationPolicy.currentRelease("2.12.0"));
    check(HubNavigationPolicy.currentRelease("3.0.0"));
    check(!HubNavigationPolicy.currentRelease("2.10.99"));
    check(!HubNavigationPolicy.currentRelease("99999999999999999999.1.0"));
    check(!HubNavigationPolicy.currentRelease(""));
    System.out.println("35 native policy checks passed");
  }
}
''', encoding='utf-8')
    result = subprocess.run([javac, '-encoding', 'UTF-8', '-d', str(tmp_path),
                             str(JAVA / 'HubNavigationPolicy.java'), str(JAVA / 'HubLocalRecordPolicy.java'), str(harness)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    result = subprocess.run([java, '-cp', str(tmp_path), 'xyz.baicang.codexhub.NativeRecordCheck'],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_record_is_bounded_plain_text_without_drafts_or_executable_content():
    browser_api = pytest.importorskip('playwright.sync_api')
    script = (ROOT / 'android/app/src/main/assets/local-record.js').read_text(encoding='utf-8')
    with browser_api.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('''<main data-ws-shell data-ws-release="2.11.0" data-user="2" data-project="demo" data-conversation="7">
            <h1>Hub</h1><div data-ws-live></div><textarea>private unsent draft</textarea>
            <input value="hidden credential" type="hidden"><img src="https://example.invalid/private.png"></main>''')
        page.evaluate('''() => {const feed=document.querySelector('[data-ws-live]');for(let i=0;i<42;i++){
          const node=document.createElement('article');node.className='ws-message';node.dataset.wsEntry=String(i);
          node.innerHTML='<div class="ws-message-meta"><strong>'+(i%2?'总控':'你')+'</strong></div><div data-ws-message-body></div>';
          node.querySelector('[data-ws-message-body]').textContent='Message '+i+' '+ 'x'.repeat(2000);feed.append(node);
        }}''')
        record = page.evaluate(script)
        assert record['user'] == '2' and record['conversation'] == '7'
        assert len(record['messages']) == 30
        assert {item['author'] for item in record['messages']} == {'你', '总控'}
        assert record['messages'][0]['text'].startswith('Message 12 ')
        assert len(record['messages'][0]['text']) == 1800
        assert 'private' not in str(record) and '<article' not in str(record)
        page.locator('[data-ws-shell]').evaluate('(node) => delete node.dataset.user')
        assert page.evaluate(script) is None
        browser.close()


def test_no_unscoped_java_interface_or_serialized_old_history_returns():
    source = (JAVA / 'MainActivity.java').read_text(encoding='utf-8')
    assert '.addJavascriptInterface(' not in source
    assert 'webView.restoreState(' not in source
    assert 'webView.goBack()' not in source
    assert 'WebViewCompat.addWebMessageListener' in source
    assert 'sourceOrigin.toString(), isMainFrame' in source
    assert '!session.equals(currentSession())' in source
    assert 'generation != recordGeneration' in source
    assert 'removeAllCookies' not in source
