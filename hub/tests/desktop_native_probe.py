"""Opt-in hidden WebView2 probe; only uses loopback and disposable browser storage.

Run twice with the same state folder and port, phase 1 then phase 2, under a
Python environment with the desktop extra installed. No production config is read.
"""
import json
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import webview
from codex_bridge.desktop_window import DesktopAPI, DesktopHost, HubLocation, offline_page


def run(state, phase, port):
    state = state.resolve()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            content = ('<!doctype html><html><body><h1>Probe ' + self.path + '</h1></body></html>').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state.mkdir(parents=True, exist_ok=True)
    external = []
    project = state / 'project'
    project.mkdir(exist_ok=True)
    host = DesktopHost(HubLocation(f'http://127.0.0.1:{port}/hub'), {'probe': project}, state,
        external_opener=external.append)
    window = webview.create_window('Hub isolated native test', html=offline_page(),
        js_api=DesktopAPI(host), hidden=True, width=1000, height=700)
    host.window = window
    window.events.before_show += host.connect_native_events
    window.events.loaded += host.on_loaded
    window.events.closing += host.close_children
    webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = False
    webview.settings['ALLOW_DOWNLOADS'] = True
    outcome = {'phase': phase, 'checks': [], 'passed': False}

    def until(check, timeout=45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                value = check()
                if value:
                    return value
            except Exception:
                pass
            time.sleep(.15)
        raise AssertionError('Probe condition timed out')

    def checks():
        try:
            until(lambda: window.get_current_url() == host.location.home)
            until(lambda: window.evaluate_js('!!(window.pywebview && window.pywebview.api.get_version)'))
            window.run_js("window.pywebview.api.get_version().then(v=>window.__version=v)")
            assert until(lambda: window.evaluate_js('window.__version'))
            outcome['checks'].append('native API ready only on trusted Hub')
            window.run_js("window.pywebview.api.list_projects().then(v=>window.__projects=v)")
            assert until(lambda: window.evaluate_js('window.__projects')) == [
                {'id': 'probe', 'name': 'project', 'available': True}]
            outcome['checks'].append('configured projects listed')
            if phase == 1:
                window.run_js("document.cookie='probe_session=persistent; Max-Age=3600; Path=/hub; SameSite=Lax'")
                assert 'probe_session=persistent' in window.evaluate_js('document.cookie')
                outcome['checks'].append('persistent profile cookie set')
                window.run_js("window.open('https://github.com/bybc-probe', '_blank')")
                until(lambda: external)
                assert external == ['https://github.com/bybc-probe']
                assert window.get_current_url() == host.location.home
                outcome['checks'].append('external new-window routed without embedded navigation')
                window.run_js("window.open('/hub/preview', '_blank')")
                until(lambda: host._children)
                child = host._children[0]
                until(lambda: child.window.get_current_url() == host.location.base + '/preview')
                assert window.get_current_url() == host.location.home
                assert 'probe_session=persistent' in child.window.evaluate_js('document.cookie')
                child.window.run_js("window.open('https://github.com/child-probe','_blank')")
                until(lambda: len(external) == 2)
                assert external[-1] == 'https://github.com/child-probe'
                child.window.destroy()
                outcome['checks'].append('preview child preserves chat and shares login; external links remain routed')
            else:
                assert 'probe_session=persistent' in window.evaluate_js('document.cookie')
                outcome['checks'].append('login cookie survived process restart')
                server.shutdown()
                server.server_close()
                host.navigate_home()
                until(lambda: window.evaluate_js('document.querySelector("h1").textContent') == '暂时连接不上 Hub')
                until(lambda: window.evaluate_js('!!(window.pywebview && window.pywebview.api.retry_connection)'))
                window.run_js("window.pywebview.api.list_projects().then(()=>window.__blocked=false).catch(()=>window.__blocked=true)")
                assert until(lambda: window.evaluate_js('window.__blocked'))
                outcome['checks'].append('offline screen blocks privileged API')
                server2 = ThreadingHTTPServer(('127.0.0.1', port), Handler)
                threading.Thread(target=server2.serve_forever, daemon=True).start()
                window.run_js("window.pywebview.api.retry_connection()")
                until(lambda: window.get_current_url() == host.location.home)
                outcome['checks'].append('offline retry reconnects to Hub')
                server2.shutdown()
                server2.server_close()
            outcome['passed'] = True
        except Exception:
            outcome['error'] = traceback.format_exc()
            outcome['page'] = window.evaluate_js('document.documentElement.outerHTML')
        finally:
            (state / f'phase-{phase}.json').write_text(json.dumps(outcome, ensure_ascii=False, indent=2), encoding='utf-8')
            window.destroy()

    webview.start(checks, gui='edgechromium', private_mode=False, storage_path=str(state / 'webview'))
    server.shutdown()
    server.server_close()
    print(json.dumps(outcome, ensure_ascii=False))
    return 0 if outcome['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(run(Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])))
