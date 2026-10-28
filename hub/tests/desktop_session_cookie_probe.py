"""Observe real Hub login retention across complete isolated WebView2 exits.

Run phases 1 and 2 as separate processes against the same state and port.
The production login endpoint is unchanged; no persistent cookie is fabricated.
"""
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn
import webview
from codex_hub.config import Settings
from codex_hub.main import create_app
from codex_bridge.desktop_window import DesktopAPI, DesktopHost, HubLocation, offline_page


def run(state, phase, port, persistent=False):
    state = state.resolve()
    state.mkdir(parents=True, exist_ok=True)
    app = create_app(Settings(data_dir=state / 'server', owner_username='probe-owner',
        owner_password='isolated-probe-password', secret_key='isolated-probe-key'))
    cookie_flags = []
    class Observe:
        async def __call__(self, scope, receive, send):
            async def observed_send(message):
                if scope.get('path') == '/login' and message['type'] == 'http.response.start':
                    for key, value in message.get('headers', []):
                        if key.lower() == b'set-cookie':
                            raw = value.decode('latin-1')
                            cookie_flags.append({'name': raw.split('=', 1)[0], 'attributes': raw.split(';')[1:]})
                await send(message)
            await app(scope, receive, observed_send)
    server = uvicorn.Server(uvicorn.Config(Observe(), host='127.0.0.1', port=port, log_level='error'))
    serving = threading.Thread(target=server.run, daemon=True)
    serving.start()
    for _ in range(100):
        if server.started: break
        time.sleep(.05)
    host = DesktopHost(HubLocation(f'http://127.0.0.1:{port}'), {}, state / 'desktop')
    window = webview.create_window('Hub isolated actual login retention probe', html=offline_page(),
        js_api=DesktopAPI(host), hidden=True)
    host.window = window
    window.events.before_show += host.connect_native_events
    window.events.loaded += host.on_loaded
    result = {'phase': phase, 'observed': False}
    def until(check, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                value = check()
                if value: return value
            except Exception:
                pass
            time.sleep(.1)
        raise AssertionError('Probe condition timed out')
    def checks():
        try:
            until(lambda: window.get_current_url() in {host.location.base + '/login', host.location.home})
            if phase == 1:
                until(lambda: window.evaluate_js("!!document.querySelector('input[name=username]')"))
                window.run_js("document.querySelector('input[name=username]').value='probe-owner';document.querySelector('input[name=password]').value='isolated-probe-password';document.querySelector('input[name=password]').form.requestSubmit();")
                until(lambda: window.get_current_url() == host.location.home)
                result['login_succeeded'] = True
                result['cookie_attributes'] = cookie_flags
                result['session_cookie_has_no_lifetime'] = all(not any('max-age' in flag.lower() or 'expires=' in flag.lower() for flag in cookie['attributes']) for cookie in cookie_flags)
                result['session_cookie_httponly'] = all(any('httponly' in flag.lower() for flag in cookie['attributes']) for cookie in cookie_flags)
                assert cookie_flags and result['session_cookie_httponly']
                assert result['session_cookie_has_no_lifetime'] is not persistent
            else:
                # Wait for the initial navigation/redirect to settle before
                # interpreting /projects as an authenticated response.
                until(lambda: window.evaluate_js("document.readyState === 'complete'"))
                time.sleep(.5)
                result['authenticated_after_restart'] = window.get_current_url() == host.location.home
                result['login_form_after_restart'] = bool(window.evaluate_js("!!document.querySelector('input[name=password]')"))
                result['location_path'] = '/projects' if result['authenticated_after_restart'] else '/login'
                if persistent and phase == 2:
                    assert result['authenticated_after_restart'] and not result['login_form_after_restart']
                    window.run_js("document.querySelector('form[action$=\"/logout\"]').requestSubmit()")
                    until(lambda: window.get_current_url() == host.location.base + '/login')
                    result['logout_succeeded'] = True
                elif persistent and phase == 3:
                    assert not result['authenticated_after_restart'] and result['login_form_after_restart']
                    result['logout_survived_restart'] = True
            result['observed'] = True
        except Exception:
            result['error'] = traceback.format_exc()
        finally:
            (state / f'session-phase-{phase}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            window.destroy()
    webview.start(checks, gui='edgechromium', private_mode=False, storage_path=str(state / 'profile'))
    server.should_exit = True
    serving.join(timeout=5)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['observed'] else 1


if __name__ == '__main__':
    code = run(Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), len(sys.argv) > 4 and sys.argv[4] == 'persistent')
    # The browser window/profile is disposed before run returns. The embedded
    # test server and pythonnet finalizers otherwise keep this test process alive.
    sys.stdout.flush()
    os._exit(code)
