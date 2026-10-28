"""Real isolated WebView2 test of bundle interception and offline continuity."""
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import webview
from codex_bridge.desktop_window import DesktopAPI, DesktopHost, HubLocation, offline_page
from codex_bridge.desktop_assets import PackagedAssets


def run(state):
    state = state.resolve()
    state.mkdir(parents=True, exist_ok=True)
    static = state / 'assets'
    static.mkdir(exist_ok=True)
    content = b'window.localAssetLoaded=true'
    (static / 'proof.js').write_bytes(content)
    (static / 'desktop-assets.json').write_text(json.dumps({'assets': {'proof.js': {'queries': ['v=1'], 'sha256': hashlib.sha256(content).hexdigest()}}}))
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(('GET', self.path))
            body = ('<!doctype html><html><head><script src="/hub/static/proof.js?v=1"></script></head><body>'
                '<main data-ws-shell data-user="1">Native cached conversation</main><input name="_csrf" value="fresh-server-token"></body></html>').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
        def do_POST(self):
            requests.append(('POST', self.path))
            self.send_response(200); self.send_header('Content-Length', '0'); self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host = DesktopHost(HubLocation(f'http://127.0.0.1:{port}/hub'), {}, state, assets=PackagedAssets(static))
    window = webview.create_window('Hub isolated local continuity probe', html=offline_page(), js_api=DesktopAPI(host), hidden=True)
    host.window = window
    window.events.before_show += host.connect_native_events
    window.events.loaded += host.on_loaded
    result = {'passed': False, 'checks': []}
    def until(check, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                value = check()
                if value: return value
            except Exception:
                pass
            time.sleep(.1)
        raise AssertionError('Timed out waiting for ' + repr(check))
    def checks():
        nonlocal server
        try:
            until(lambda: window.evaluate_js('window.localAssetLoaded'))
            until(lambda: host.cache.get(host.location.home))
            assert not any('/static/' in path for method, path in requests)
            result['checks'].append('static script served by native bundle without network request')
            server.shutdown(); server.server_close()
            host.navigate_home()
            until(lambda: window.evaluate_js("document.documentElement.dataset.hubOffline === 'true'"))
            assert window.get_current_url() == host.location.home
            assert window.evaluate_js('window.localAssetLoaded') is True
            assert 'Native cached conversation' in window.evaluate_js('document.body.textContent')
            assert window.evaluate_js("document.querySelector('[name=_csrf]').value") == ''
            result['checks'].append('network failure preserves origin, cached conversation and bundled script, with offline banner and no CSRF')
            window.run_js("fetch('/hub/projects/dispatch',{method:'POST'}).then(r=>window.writeStatus=r.status)")
            assert until(lambda: window.evaluate_js('window.writeStatus')) == 503
            assert not any(method == 'POST' for method, path in requests)
            result['checks'].append('offline writes return 503 and never reach server')
            server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            host.cache.latest_verified = None
            window.run_js("fetch(location.href,{cache:'no-store',headers:{'X-Hub-Online-Probe':'1'}}).then(r=>r.text()).then(t=>window.freshBody=t)")
            until(lambda: host.cache.latest_verified)
            until(lambda: window.evaluate_js('!!window.freshBody'))
            assert 'fresh-server-token' in window.evaluate_js('window.freshBody')
            window.run_js("pywebview.api.workspace_online('1').then(r=>window.recovered=r.online)")
            until(lambda: window.evaluate_js('window.recovered'))
            assert not host._offline
            window.run_js("fetch('/hub/projects/dispatch',{method:'POST'}).then(r=>window.onlineWriteStatus=r.status)")
            assert until(lambda: window.evaluate_js('window.onlineWriteStatus')) == 200
            result['checks'].append('fresh same-account server GET unlocks writes without replacing page')
            window.run_js("fetch('/hub/logout',{method:'POST'})")
            until(lambda: host.cache.get('', latest=True) is None)
            result['checks'].append('logout clears current account snapshots')
            result['passed'] = True
        except Exception:
            result['error'] = traceback.format_exc()
        finally:
            (state / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            window.destroy()
    webview.start(checks, gui='edgechromium', private_mode=False, storage_path=str(state / 'webview'))
    server.shutdown(); server.server_close()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(run(Path(sys.argv[1])))
