"""Current-workspace Android behavior and executable native URL/download boundaries."""
from pathlib import Path
import os
import shutil
import subprocess

import pytest

from test_mobile_composer_browser import live_hub  # noqa: F401
from test_workspace_shell_browser import login_page
from workspace_fixture import seed_workspace

ROOT = Path(__file__).resolve().parents[2]
ANDROID = ROOT / "codex-hub/android/app/src/main"
JAVA = ANDROID / "java/xyz/baicang/codexhub"
playwright = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("width", [360, 390])
def test_current_workspace_native_back_download_and_import_batch(live_hub, repo, width):
    data = seed_workspace(repo, room_messages=2)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": width, "height": 800}, is_mobile=True, has_touch=True)
            login_page(page, live_hub)
            page.goto(f'{live_hub}/projects/workspace-team/conversations/{data["conversation_id"]}')
            page.locator("[data-ws-input]").fill("手机草稿必须保留")
            page.evaluate("""() => {
              window.__downloads = []; window.__cleared = [];
              window.HubApp = {download: url => __downloads.push(url)};
              window.HubLocalData = {clearImportedFilesFor: ids => __cleared.push(JSON.parse(ids))};
            }""")
            page.add_script_tag(path=str(ANDROID / "assets/android-workspace.js"))
            page.add_script_tag(path=str(ANDROID / "assets/android-workspace.js"))
            page.locator("[data-ws-sidebar-open]").click()
            assert page.evaluate("window.CodexHubAndroid.back()") is True
            assert not page.locator("[data-ws-shell]").evaluate("node => node.classList.contains('is-sidebar-open')")
            page.locator("[data-ws-inspector-open]").click()
            assert page.evaluate("window.CodexHubAndroid.back()") is True
            assert page.evaluate("window.CodexHubAndroid.back()") is False
            previous_url = page.url
            page.evaluate("""() => {
              const link = document.createElement('a'); link.href = '/projects/workspace-team/attachments/91';
              link.download = ''; link.textContent = '下载'; document.body.append(link); link.click();
            }""")
            assert page.url == previous_url
            assert len(page.evaluate("__downloads")) == 1
            assert page.locator("[data-ws-input]").input_value() == "手机草稿必须保留"
            # Capture A without submitting real work. A late receipt must not consume B.
            page.evaluate("""() => {
              const form = document.querySelector('[data-ws-dispatch]');
              form.addEventListener('submit', event => {event.preventDefault(); event.stopImmediatePropagation();}, true);
              CodexHubAndroidFiles.receiveSelection([{name:'A.png',size:5,importId:'opaque-A'}]);
              form.elements.request_key.value = 'request-A';
              form.dispatchEvent(new Event('submit', {bubbles:true,cancelable:true}));
              CodexHubAndroidFiles.receiveSelection([{name:'B.png',size:7,importId:'opaque-B'}]);
              window.dispatchEvent(new CustomEvent('hub:workspace-sent', {detail:{requestKey:'request-A'}}));
              window.dispatchEvent(new CustomEvent('hub:workspace-sent', {detail:{requestKey:'request-A'}}));
              window.dispatchEvent(new CustomEvent('hub:workspace-sent', {detail:{requestKey:'unknown-restored-job'}}));
            }""")
            assert page.evaluate("__cleared") == [["opaque-A"]]
            page.evaluate("""() => {
              const form = document.querySelector('[data-ws-dispatch]');
              form.elements.request_key.value = 'request-B';
              form.dispatchEvent(new Event('submit', {bubbles:true,cancelable:true}));
              window.dispatchEvent(new CustomEvent('hub:workspace-sent', {detail:{requestKey:'request-B'}}));
            }""")
            assert page.evaluate("__cleared") == [["opaque-A"], ["opaque-B"]]
        finally:
            browser.close()


def test_android_native_download_and_navigation_policies_execute(tmp_path):
    suffix = ".exe" if os.name == "nt" else ""
    jdk = ROOT / ".local-tools/jdk-17.0.19+10/bin"
    javac = str(jdk / f"javac{suffix}") if jdk.exists() else shutil.which("javac")
    java = str(jdk / f"java{suffix}") if jdk.exists() else shutil.which("java")
    if not java or not javac:
        pytest.skip("JDK unavailable for executable native checks")
    harness = tmp_path / "MobilePolicyCheck.java"
    harness.write_text(r'''
package xyz.baicang.codexhub;
import java.io.*;
import java.net.*;
import java.nio.file.*;
import java.util.*;
public final class MobilePolicyCheck {
    static final String BASE = "https://example.invalid/hub/";
    static final String FILE = BASE + "projects/demo/attachments/7";
    static void check(boolean condition) { if (!condition) throw new AssertionError(); }
    static final class Reply extends HttpURLConnection {
        int status = 200; byte[] bytes = {1,2,3,4}; boolean closed;
        Map<String,String> headers = new HashMap<>();
        Reply() throws Exception { super(new URL(FILE)); headers.put("Content-Length", "4"); headers.put("Content-Type", "image/png"); }
        public void connect() {} public void disconnect() {closed=true;} public boolean usingProxy() {return false;}
        public int getResponseCode() {return status;}
        public String getHeaderField(String key) {return headers.get(key);}
        public String getContentType() {return headers.get("Content-Type");}
        public InputStream getInputStream() {return new ByteArrayInputStream(bytes);}
    }
    static void denied(Reply reply, File directory) throws Exception {
        boolean failed = false;
        try { HubFileDownload.fetch(BASE, FILE, "session=private", "ua", directory, uri -> reply); }
        catch (IOException expected) {failed=true;}
        check(failed && reply.closed); check(directory.listFiles().length == 0);
    }
    public static void main(String[] args) throws Exception {
        check(HubNavigationPolicy.workspace(BASE, BASE+"projects/demo/conversations/31?task_history=1"));
        check(HubNavigationPolicy.workspace(BASE, BASE+"projects/demo"));
        for (String value : Arrays.asList("https://example.invalid:444/hub/projects/demo", "https://evil.invalid/hub/projects/demo",
                "https://user@example.invalid/hub/projects/demo", BASE+"projects/%2e%2e", BASE+"api/projects/demo", BASE+"projects/demo/attachments/7")) {
            check(!HubNavigationPolicy.workspace(BASE, value));
        }
        check(!HubNavigationPolicy.recoverable(BASE, FILE));
        check(!HubNavigationPolicy.recoverable(BASE, BASE+"downloads/android"));
        check(HubNavigationPolicy.recoverable(BASE, BASE+"login"));
        check(HubNavigationPolicy.recoverable(BASE, BASE+"projects/demo/attachments/7/preview"));
        File directory = new File(args[0]); directory.mkdirs();
        Reply ok = new Reply(); ok.headers.put("Content-Disposition", "attachment; filename*=UTF-8''%E6%96%87%E4%BB%B6%2B.png");
        HubFileDownload.Result result = HubFileDownload.fetch(BASE, FILE, "session=private", "ua", directory, uri -> ok);
        check(result.bytes==4 && result.name.equals("文件+.png") && result.type.equals("image/png"));
        check(Arrays.equals(Files.readAllBytes(result.file.toPath()), ok.bytes));
        check(ok.getRequestProperty("Cookie").equals("session=private") && !ok.getInstanceFollowRedirects());
        check(ok.closed); result.file.delete();
        Reply shortFile = new Reply(); shortFile.headers.put("Content-Length", "99"); denied(shortFile,directory);
        Reply huge = new Reply(); huge.headers.put("Content-Length", Long.toString(HubFileDownload.MAX_BYTES+1)); denied(huge,directory);
        Reply login = new Reply(); login.headers.put("Content-Type", "text/html"); denied(login,directory);
        Reply expired = new Reply(); expired.status=302; expired.headers.put("Location", BASE+"login"); denied(expired,directory);
        Reply foreign = new Reply(); foreign.status=302; foreign.headers.put("Location", "https://evil.invalid/file"); denied(foreign,directory);
        Reply relative = new Reply(); relative.status=302; relative.headers.put("Location", "/hub/projects/demo/attachments/8");
        Reply finalReply = new Reply(); final int[] calls={0};
        result=HubFileDownload.fetch(BASE, FILE, "session=private", "ua", directory, uri -> calls[0]++==0?relative:finalReply);
        check(calls[0]==2 && relative.closed && finalReply.closed); result.file.delete();
        check(directory.listFiles().length==0);
        System.out.println("mobile-policy: passed");
    }
}
''', encoding="utf-8")
    compile_result = subprocess.run([javac, "-encoding", "UTF-8", "-d", str(tmp_path),
        *(str(JAVA / name) for name in ["SharedFilePolicy.java", "HubNavigationPolicy.java", "HubFileDownload.java"]),
        str(harness)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    assert compile_result.returncode == 0, compile_result.stdout + compile_result.stderr
    result = subprocess.run([java, "-cp", str(tmp_path), "xyz.baicang.codexhub.MobilePolicyCheck", str(tmp_path / "files")],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "mobile-policy: passed" in result.stdout
