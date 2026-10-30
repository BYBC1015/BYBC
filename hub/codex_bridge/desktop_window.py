"""Hub's desktop foreground; independent of the Bridge execution service.

Windows uses pywebview 6.2 / WebView2. Only the configured Hub origin and path
can remain in this window. No device credential is exposed to the web bridge.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import html
import ipaddress
import json
import os
from functools import lru_cache
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from . import __version__
from .config import default_config_path, hub_workspace_url, load_config
from .persistent_log import PersistentLog
from .single_instance import SingleInstance
from .subprocess_utils import hidden_subprocess_kwargs
from .desktop_assets import PackagedAssets, WorkspaceCache, canonical_url


TITLE = 'BYBC Hub'
DOMESTIC_DOMAINS = frozenset({'baidu.com', 'bilibili.com', 'qq.com', 'weixin.com',
    'weibo.com', 'zhihu.com', 'xiaohongshu.com', 'douyin.com', 'feishu.cn',
    'aliyun.com', 'taobao.com', 'jd.com', 'kimi.com', 'deepseek.com'})


class HubLocation:
    def __init__(self, hub: str):
        value = urlsplit(str(hub).strip())
        if value.scheme not in {'https', 'http'} or not value.hostname or value.username or value.password:
            raise ValueError('Hub 地址需为不含账号密码的 HTTP 或 HTTPS 地址。')
        self.scheme = value.scheme.lower()
        self.host = value.hostname.lower()
        self.port = value.port or (443 if self.scheme == 'https' else 80)
        self.path = value.path.rstrip('/')
        if '\\' in self.path or '..' in unquote(self.path).split('/'):
            raise ValueError('Hub 地址路径无效。')
        self.base = urlunsplit((value.scheme, value.netloc, self.path, '', ''))
        self.home = hub_workspace_url(self.base)
        self.key = hashlib.sha256(self.base.encode()).hexdigest()[:20]

    def trusted(self, url: str | None) -> bool:
        try:
            value = urlsplit(url or '')
            path = unquote(value.path)
            return (value.scheme == self.scheme and (value.hostname or '').lower() == self.host
                and (value.port or (443 if value.scheme == 'https' else 80)) == self.port
                and not value.username and not value.password and '\\' not in path
                and '..' not in path.split('/')
                and (path == self.path or path.startswith(self.path + '/')))
        except (ValueError, TypeError):
            return False


def browser_for(url: str) -> str:
    value = urlsplit(url)
    if value.scheme not in {'http', 'https'} or not value.hostname or value.username or value.password:
        raise ValueError('只可在浏览器打开普通网页链接。')
    host = value.hostname.lower()
    try:
        if not ipaddress.ip_address(host).is_global:
            return 'edge'
    except ValueError:
        pass
    return 'edge' if host == 'localhost' or host.endswith('.cn') or any(host == domain or host.endswith('.' + domain)
        for domain in DOMESTIC_DOMAINS) else 'chrome'


def browser_executable(browser: str) -> Path:
    suffix = Path('Google/Chrome/Application/chrome.exe') if browser == 'chrome' else Path('Microsoft/Edge/Application/msedge.exe')
    for variable in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        base = os.environ.get(variable)
        if base and (Path(base) / suffix).is_file():
            return Path(base) / suffix
    raise FileNotFoundError('未找到谷歌浏览器。' if browser == 'chrome' else '未找到 Microsoft Edge。')


def open_external(url: str) -> None:
    browser = browser_for(url)
    subprocess.Popen([str(browser_executable(browser)), '--new-tab', url], **visible_gui_subprocess_kwargs())


def visible_gui_subprocess_kwargs() -> dict:
    # SW_HIDE suppresses a WinForms app's first Show. Suppress only a console;
    # HubDesktop and the requested browser must show their actual GUI normally.
    return {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}


def desktop_state_dir(location: HubLocation) -> Path:
    return Path(os.environ.get('APPDATA') or Path.home()) / 'CodexHubDesktop' / location.key


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def read_geometry(path: Path, screens=()) -> dict:
    defaults = {'width': 1380, 'height': 900, 'x': None, 'y': None, 'maximized': False}
    try:
        if path.stat().st_size > 4096:
            return defaults
        saved = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(saved, dict):
            return defaults
        for key, minimum, maximum in [('width', 900, 3840), ('height', 620, 2160)]:
            if type(saved.get(key)) is int:
                defaults[key] = max(minimum, min(maximum, saved[key]))
        if type(saved.get('x')) is int and type(saved.get('y')) is int:
            x, y = saved['x'], saved['y']
            # Keep a usable title bar on a currently connected display.
            visible = not screens or any(x + 100 > screen.x and x < screen.x + screen.width - 100
                and y >= screen.y and y < screen.y + screen.height - 80 for screen in screens)
            if visible and -20000 <= x <= 20000 and -20000 <= y <= 20000:
                defaults.update(x=x, y=y)
        defaults['maximized'] = saved.get('maximized') is True
    except (OSError, ValueError, TypeError):
        pass
    if screens:
        # A saved window on a larger secondary display must not be shrunk to
        # the primary monitor's dimensions on every launch.
        screen = next((item for item in screens if defaults['x'] is not None
            and defaults['x'] + 100 > item.x and defaults['x'] < item.x + item.width - 100
            and item.y <= defaults['y'] < item.y + item.height - 80), screens[0])
        defaults['width'] = min(defaults['width'], max(900, int(screen.width)))
        defaults['height'] = min(defaults['height'], max(620, int(screen.height)))
    return defaults


@lru_cache(maxsize=1)
def _offline_brand_markup() -> str:
    # The compact, shipped brand works before any network or WebView session.
    # Keep the built-in data document under the native navigation size limit.
    try:
        logo = Path(__file__).with_name('assets').joinpath('codex-owl-logo-lite.png').read_bytes()
        if not logo.startswith(b'\x89PNG\r\n\x1a\n') or len(logo) > 98304:
            raise ValueError('Invalid compact brand resource')
        return '<img alt="" width="62" height="62" src="data:image/png;base64,' + base64.b64encode(logo).decode('ascii') + '">'
    except (OSError, ValueError):
        return '<span class="brand-fallback">BYBC</span>'


def offline_page(message='正在连接你的工作区…', *, connecting=True) -> str:
    brand = _offline_brand_markup()
    title = '正在打开 Hub' if connecting else '暂时连接不上 Hub'
    detail = '聊天、智能体和成果，即将在这里继续。' if connecting else '恢复连接后继续当前页面。登录信息保留在本机；后台工作的实际进度，以重新连接后的记录为准。'
    state = '连接工作区' if connecting else '等待重新连接'
    modifier = ' connecting' if connecting else ''
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<style>*{{box-sizing:border-box}}html,body{{margin:0;min-height:100%;background:#151819;color:#edf0ed;font:16px "Noto Sans SC","PingFang SC","Microsoft YaHei UI","Microsoft YaHei","Noto Sans CJK SC","Segoe UI",sans-serif}}body{{display:grid;place-items:center;min-height:100vh;padding:32px;background:radial-gradient(ellipse at 50% 20%,#27322e70,transparent 60%),#151819}}main{{width:min(540px,100%);padding:40px;border:1px solid #3a444040;border-radius:32px;background:#202725;box-shadow:0 28px 90px #0003}}header{{display:flex;align-items:center;justify-content:space-between;gap:20px}}.mark{{display:grid;place-items:center;width:62px;height:62px;border-radius:22px;background:#cfdbc512;color:#d3dfcb;overflow:hidden;font-weight:600}}.mark img{{display:block;width:100%;height:100%;object-fit:contain}}.brand-fallback{{font-size:17px}}.state{{display:flex;align-items:center;gap:9px;font-size:12px;color:#a7b5ad}}.dot{{width:7px;height:7px;border-radius:50%;background:#c6ba96}}.connecting .dot{{background:#b7cbb4;animation:breathe 2.4s ease-in-out infinite}}h1{{font-size:27px;font-weight:600;letter-spacing:.01em;margin:34px 0 16px}}p{{line-height:1.9;color:#b6c1bb;margin:0}}#status{{min-height:28px;margin-top:24px;font-size:13px;color:#ced7cf}}button{{width:100%;margin-top:20px;border:1px solid transparent;border-radius:18px;padding:14px 23px;background:#cbd9c5;color:#1b2a20;font:inherit;font-weight:600;cursor:pointer;transition:background .15s,transform .15s}}button:hover{{background:#dce7d7}}button:active{{transform:translateY(1px)}}button:focus-visible{{outline:2px solid #e2f1db;outline-offset:4px}}button:disabled{{opacity:.6;cursor:wait}}small{{display:block;text-align:center;margin-top:24px;color:#8c9a91;font-size:11px;letter-spacing:.09em}}@keyframes breathe{{50%{{opacity:.35}}}}@media(prefers-reduced-motion:reduce){{*,*::before,*::after{{animation:none!important;transition:none!important}}}}@media(max-width:480px){{body{{padding:18px}}main{{padding:28px;border-radius:26px}}h1{{font-size:24px}}}}</style>
<main class="{modifier.strip()}"><header><div class="mark" aria-hidden="true">{brand}</div><span class="state"><span class="dot" aria-hidden="true"></span>{state}</span></header><h1>{title}</h1><p>{detail}</p><p id="status" role="status" aria-live="polite">{html.escape(message)}</p><button id="retry-button" type="button" onclick="retry()">重新连接</button><small>BYBC HUB · 你的智能体工作区</small></main>
<script>let retryPending=false;async function retry(){{if(retryPending)return;const status=document.querySelector('#status'),button=document.querySelector('#retry-button');const connect=window.pywebview?.api?.retry_connection;if(typeof connect!=='function'){{status.textContent='桌面连接仍在准备，请稍后重试。';return;}}retryPending=true;button.disabled=true;status.textContent='正在重新连接…';try{{const result=await connect();if(!result?.connecting)throw new Error('unavailable');}}catch{{status.textContent='连接尚未恢复，请稍后重试。';}}finally{{retryPending=false;button.disabled=false;}}}}window.addEventListener('online',retry);</script></html>'''


class DesktopInstance:
    """One foreground per Hub; a second launch only signals the existing window."""
    def __init__(self, key: str):
        self.mutex = SingleInstance(r'Local\BYBC.HubDesktop.' + key)
        self.event_name = r'Local\BYBC.HubDesktop.Wake.' + key
        self.close_name = r'Local\BYBC.HubDesktop.Close.' + key
        self.event = None
        self.close_event = None
        self.stopped = threading.Event()
        self._kernel = None

    def acquire(self) -> bool:
        owned = self.mutex.acquire()
        if sys.platform != 'win32':
            return owned
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateEventW.restype = wintypes.HANDLE
        kernel.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.OpenEventW.restype = wintypes.HANDLE
        kernel.SetEvent.argtypes = [wintypes.HANDLE]
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel = kernel
        if owned:
            self.event = kernel.CreateEventW(None, False, False, self.event_name)
            self.close_event = kernel.CreateEventW(None, False, False, self.close_name)
            if not self.event or not self.close_event:
                self.close()
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            for _ in range(10):
                handle = kernel.OpenEventW(0x0002, False, self.event_name)
                if handle:
                    kernel.SetEvent(handle); kernel.CloseHandle(handle)
                    break
                time.sleep(.1)
        return owned

    def watch(self, callback, close_callback=None) -> None:
        if not self.event:
            return
        from ctypes import wintypes
        handles = (wintypes.HANDLE * 2)(self.event, self.close_event)
        while not self.stopped.is_set() and self.event:
            result = self._kernel.WaitForMultipleObjects(2, handles, False, 500)
            if self.stopped.is_set():
                break
            if result == 0:
                callback()
            elif result == 1 and close_callback:
                close_callback()
                break

    def request_close(self) -> bool:
        if sys.platform != 'win32':
            return False
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.OpenEventW.restype = wintypes.HANDLE
        kernel.SetEvent.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenEventW(0x0002, False, self.close_name)
        if not handle:
            return False
        try:
            return bool(kernel.SetEvent(handle))
        finally:
            kernel.CloseHandle(handle)

    def close(self) -> None:
        self.stopped.set()
        if self.event:
            self._kernel.SetEvent(self.event)
            # The watcher owns no work; it exits before we release its handle.
            watcher = getattr(self, '_watcher', None)
            if watcher and watcher is not threading.current_thread():
                watcher.join(timeout=2)
            self._kernel.CloseHandle(self.event)
            self.event = None
        if self.close_event:
            self._kernel.CloseHandle(self.close_event)
            self.close_event = None
        self.mutex.close()


class DesktopAPI:
    def __init__(self, host):
        self._host = host

    def info(self):
        self._host.require_trusted()
        return {'name': TITLE, 'version': __version__, 'local_projects': len(self._host.projects)}

    def get_version(self):
        self._host.require_trusted()
        return __version__

    def connection_state(self):
        self._host.require_trusted(allow_local=True)
        return {'offline': self._host._offline, 'cached_workspace': bool(self._host.cache.get('', latest=True))}

    def workspace_online(self, user_id=None):
        self._host.require_trusted()
        return {'online': self._host.confirm_workspace_online(user_id)}

    def list_projects(self):
        self._host.require_trusted()
        return [{'id': slug, 'name': path.name, 'available': path.is_dir()}
                for slug, path in self._host.projects.items()]

    def open_project(self, project_id):
        self._host.require_trusted()
        self._host.open_project(project_id)
        return {'opened': True}

    def retry_connection(self):
        self._host.require_trusted(allow_local=True)
        self._host.schedule_reconnect()
        return {'connecting': True}


class DesktopHost:
    def __init__(self, location: HubLocation, projects: dict, state_dir: Path, *, external_opener=open_external, cache=None, assets=None):
        self.location = location
        self.projects = {str(slug): Path(path).expanduser().resolve() for slug, path in projects.items()}
        self.state_dir = state_dir
        self.window = None
        self.log = PersistentLog(state_dir / 'desktop.log')
        self.external_opener = external_opener
        self._local_page = True
        self._local_html = offline_page()
        self._first_load = True
        self._active_navigation = None
        self._cancelled_navigations = set()
        self._failed_navigations = set()
        self._recovery_target = None
        self._geometry_lock = threading.Lock()
        self._geometry = read_geometry(state_dir / 'window.json')
        self._geometry_timer = None
        self._geometry_serial = 0
        self._geometry_dirty = False
        self._children = []
        self._lifecycle_lock = threading.RLock()
        self._closed = threading.Event()
        self._retry_timer = None
        self._retry_serial = 0
        self._native_ready = False
        self.cache = cache or WorkspaceCache(state_dir / 'workspace-cache')
        self.assets = assets or PackagedAssets()
        self._offline = False
        self._offline_since = 0
        self._offline_account = None
        self._offline_response = None
        self._navigation_target = location.home
        self._resume_url = location.home
        self._login_network_ready = False

    def require_trusted(self, *, allow_local=False):
        if self._closed.is_set():
            raise PermissionError('窗口已关闭。')
        current = self.window.get_current_url()
        if self.location.trusted(current):
            return
        if allow_local and self._local_page and current in (None, '', 'about:blank', 'about:srcdoc'):
            return
        raise PermissionError('当前页面不能使用本机工作区接口。')

    def open_project(self, project_id):
        if not isinstance(project_id, str) or project_id not in self.projects:
            raise ValueError('请选择已配置的本机工程。')
        path = self.projects[project_id]
        if not path.is_dir():
            raise FileNotFoundError('本机工程目录暂不可用。')
        os.startfile(str(path))

    def bring_forward(self):
        if self._closed.is_set():
            return
        self.window.show()
        if self._closed.is_set():
            return
        if str(self.window.native.WindowState) == 'Minimized':
            if self._geometry.get('maximized'):
                self.window.maximize()
            else:
                self.window.restore()

    def navigate_home(self):
        self._navigate(self.location.home)

    def reconnect(self):
        self._navigate(self._resume_url)

    def schedule_reconnect(self):
        # Resolve the bridge Promise first. Repeated clicks replace one pending
        # retry, and even a callback already queued before close becomes inert.
        with self._lifecycle_lock:
            if self._closed.is_set():
                return
            self._retry_serial += 1
            serial = self._retry_serial
            if self._retry_timer:
                self._retry_timer.cancel()
            def retry():
                with self._lifecycle_lock:
                    if self._closed.is_set() or serial != self._retry_serial:
                        return
                    self._retry_timer = None
                self.reconnect()
            self._retry_timer = threading.Timer(.2, retry)
            self._retry_timer.daemon = True
            self._retry_timer.start()

    def _cancel_reconnect(self):
        with self._lifecycle_lock:
            self._retry_serial += 1
            if self._retry_timer:
                self._retry_timer.cancel()
                self._retry_timer = None

    def _navigate(self, target):
        if self._closed.is_set():
            return
        self._cancel_reconnect()
        self._recovery_target = None
        self._offline_response = None
        self._local_page = False
        self._navigation_target = target
        if self._can_resume(target):
            self._resume_url = target
        # WebView2 setting Source to the current URL does not reload it.
        if self.window.get_current_url() == target:
            self.window.run_js('window.location.reload()')
        else:
            self.window.load_url(target)

    def _can_resume(self, target):
        if not self.location.trusted(target):
            return False
        value = urlsplit(target)
        parts = unquote(value.path)[len(self.location.path):].strip('/').split('/')
        query = parse_qs(value.query, keep_blank_values=True)
        if 'download' in query or parts[0] in {'login', 'logout', 'api', 'static', 'downloads'}:
            return False
        if parts[0] != 'projects':
            return parts[0] in {'', 'inbox', 'devices', 'feedback', 'notifications'}
        if 'attachments' in parts:
            return parts[-1] == 'preview' or query.get('inline') == ['true']
        return not parts[-1].endswith(('.md', '.json')) and parts[-1] != 'chat-updates'

    def show_offline(self, message):
        if self._closed.is_set():
            return
        self._offline = True
        self._offline_since = time.time()
        self._local_page = True
        self._local_html = offline_page(message, connecting=False)
        self.window.load_html(self._local_html)

    def is_local_html_navigation(self, target):
        # WebView2 NavigateToString exposes our HTML as a data: navigation event.
        # Allow only the exact built-in document, never arbitrary data pages.
        prefix = 'data:text/html;charset=utf-8;base64,'
        if not self._local_page or not target.startswith(prefix) or len(target) > 262144:
            return False
        try:
            return base64.b64decode(target[len(prefix):], validate=True) == self._local_html.encode('utf-8')
        except ValueError:
            return False

    def on_loaded(self):
        if self._closed.is_set():
            return
        if self._first_load:
            self._first_load = False
            self.navigate_home()
        elif not self._local_page and self.location.trusted(self.window.get_current_url()):
            self.window.run_js("document.documentElement.dataset.hubDesktop='true';window.dispatchEvent(new Event('hubdesktopready'));")

    def on_navigation(self, sender, args):
        if self._closed.is_set():
            args.Cancel = True
            return
        target = str(args.Uri)
        if (target in ('about:blank', 'about:srcdoc') and self._local_page) or self.is_local_html_navigation(target):
            self._active_navigation = int(args.NavigationId)
            return
        if self.location.trusted(target):
            self._cancel_reconnect()
            self._active_navigation = int(args.NavigationId)
            self._navigation_target = target
            if self._can_resume(target):
                self._resume_url = target
            self._local_page = False
            return
        args.Cancel = True
        self._cancelled_navigations.add(int(args.NavigationId))
        # Native navigation events also cover forms, redirects and target=_blank.
        # Unsupported schemes never launch a local application.
        if target.startswith(('https://', 'http://')):
            self.launch_external(target)

    def launch_external(self, target):
        def launch():
            if self._closed.is_set():
                return
            try:
                self.external_opener(target)
            except (OSError, ValueError):
                self.log.write('External page could not be opened in the configured browser.')
                if not self._closed.is_set() and self.location.trusted(self.window.get_current_url()):
                    self.window.run_js("alert('未能打开链接，请检查本机谷歌浏览器或 Microsoft Edge 是否已安装。')")
        threading.Thread(target=launch, daemon=True).start()

    def on_new_window(self, sender, args):
        args.Handled = True
        if self._closed.is_set():
            return
        target = str(args.Uri)
        if self.location.trusted(target):
            threading.Thread(target=self.open_preview, args=(target,), daemon=True).start()
        elif target.startswith(('https://', 'http://')):
            self.launch_external(target)

    def open_preview(self, target):
        if self._closed.is_set() or not self.location.trusted(target):
            return
        import webview
        from webview.menu import Menu, MenuAction
        # A child preserves the chat's draft/File objects, including during login.
        child = DesktopHost(self.location, self.projects, self.state_dir, external_opener=self.external_opener,
            cache=self.cache, assets=self.assets)
        child._first_load = False
        window = webview.create_window(TITLE + ' · 预览', html=offline_page(), js_api=DesktopAPI(child),
            width=1100, height=800, min_size=(700, 500), background_color='#151819',
            hidden=self.window.hidden,
            menu=[Menu('Hub', [MenuAction('回到聊天窗口', self.bring_forward),
                MenuAction('上一页', lambda: child.window.run_js('history.back()'))])])
        child.window = window
        # Window creation can yield to the native event loop. The parent may
        # have closed during it; never leave that late preview orphaned.
        with self._lifecycle_lock:
            closed = self._closed.is_set()
            if not closed:
                self._children.append(child)
        if closed:
            child.close_children()
            window.destroy()
            return
        window.events.loaded += child.on_loaded
        # Dynamically created pywebview windows have already emitted before_show.
        # Attach interception before sending this child's first remote navigation.
        child.connect_native_events()
        def forget_child():
            child.close_children()
            with self._lifecycle_lock:
                if child in self._children:
                    self._children.remove(child)
        window.events.closed += forget_child
        window.events.closing += child.close_children
        if child._closed.is_set() or self._closed.is_set():
            return
        child._local_page = False
        child._navigate(target)

    def close_children(self):
        with self._lifecycle_lock:
            if self._closed.is_set():
                return
            self._closed.set()
            self._retry_serial += 1
            if self._retry_timer:
                self._retry_timer.cancel()
                self._retry_timer = None
            children = list(self._children)
            self._children.clear()
        self._flush_geometry()
        for child in children:
            child.close_children()
            child.window.destroy()

    def on_download_starting(self, sender, args):
        # A navigation converted to a download is not a disconnected Hub.
        # Downloads from another frame/older page must not suppress the current
        # navigation's genuine network failure.
        uri = str(getattr(getattr(args, 'DownloadOperation', None), 'Uri', ''))
        if (not self._closed.is_set() and self._active_navigation is not None and uri
                and canonical_url(uri) == canonical_url(self._navigation_target)):
            self._cancelled_navigations.add(self._active_navigation)

    def on_native_ready(self, sender=None, args=None):
        if self._closed.is_set() or self._native_ready or (args is not None and not args.IsSuccess):
            return
        self._native_ready = True
        control = self.window.native.webview
        # Replace pywebview's same-window popup policy after it initialized.
        control.CoreWebView2.NewWindowRequested -= self.window.native.browser.on_new_window_request
        control.CoreWebView2.NewWindowRequested += self.on_new_window
        control.CoreWebView2.DownloadStarting += self.on_download_starting
        from Microsoft.Web.WebView2.Core import CoreWebView2WebResourceContext
        control.CoreWebView2.AddWebResourceRequestedFilter(self.location.base + '/*', CoreWebView2WebResourceContext.All)
        control.CoreWebView2.WebResourceRequested += self.on_resource_requested
        control.CoreWebView2.WebResourceResponseReceived += self.on_resource_response

    def on_navigation_completed(self, sender, args):
        if self._closed.is_set():
            return
        navigation_id = int(args.NavigationId)
        if navigation_id in self._cancelled_navigations:
            self._cancelled_navigations.discard(navigation_id)
            self._failed_navigations.add(navigation_id)
            return
        if navigation_id == self._active_navigation and not args.IsSuccess:
            if (self._local_page or navigation_id in self._failed_navigations or
                    str(getattr(args, 'WebErrorStatus', '')).endswith('OperationCanceled')):
                return
            self._failed_navigations.add(navigation_id)
            if len(self._failed_navigations) > 64:
                self._failed_navigations.remove(min(self._failed_navigations))
            target = canonical_url(self._navigation_target)
            if target == self._recovery_target:
                # If the cached-document navigation itself fails, loading it
                # again recursively can replace a usable chat and spin forever.
                self.show_offline('页面未能打开。你的工作记录仍保留，请重新连接后重试。')
                return
            self._recovery_target = target
            self.log.write('Hub navigation failed; restoring local workspace if available.')
            threading.Thread(target=self.restore_workspace, args=(navigation_id, self._navigation_target), daemon=True).start()

    def restore_workspace(self, navigation_id=None, target=None):
        if self._closed.is_set():
            return
        navigation_id = self._active_navigation if navigation_id is None else navigation_id
        if self._closed.is_set() or navigation_id != self._active_navigation:
            return
        target = canonical_url(target or self._navigation_target)
        snapshot = self.cache.get(target)
        if snapshot is None and target == self.location.home:
            snapshot = self.cache.get(target, latest=True)
        if self._closed.is_set() or navigation_id != self._active_navigation:
            return
        if snapshot is None:
            self.show_offline('网络连接未完成，请检查连接后重试。')
            return
        self._offline = True
        self._offline_since = time.time()
        self._offline_account = snapshot['account']
        self._offline_response = snapshot
        if self._closed.is_set() or navigation_id != self._active_navigation:
            self._offline_response = None
            return
        if self.window.get_current_url() == snapshot['url']:
            self.window.run_js('window.location.reload()')
        else:
            self.window.load_url(snapshot['url'])

    def confirm_workspace_online(self, user_id=None):
        if self._closed.is_set():
            return False
        verified = self.cache.latest_verified
        if not verified or time.time() - verified['time'] > 30:
            return False
        if self._offline and verified['time'] < self._offline_since:
            return False
        user = self.window.evaluate_js("document.querySelector('[data-ws-shell]')?.dataset.user || ''")
        if self._closed.is_set() or not isinstance(user, str) or user != verified['user']:
            return False
        if user_id is not None and str(user_id) != user:
            return False
        cached_document = self.window.evaluate_js("document.documentElement.dataset.hubOffline === 'true'")
        if cached_document and self._offline_account and verified['account'] != self._offline_account:
            return False
        # Another window may have logged out or changed account during the DOM
        # query. Do not let that earlier response unlock this page afterwards.
        if (self._closed.is_set() or verified is not self.cache.latest_verified
                or verified.get('generation') != self.cache.generation):
            return False
        self._offline = False
        self._offline_account = None
        self._recovery_target = None
        self.window.run_js("window.__HUB_DESKTOP_OFFLINE__=false;delete document.documentElement.dataset.hubOffline;document.getElementById('hub-desktop-offline-banner')?.remove();window.dispatchEvent(new Event('hubdesktopconnectionchange'));")
        return True

    def _resource_response(self, body, mime, status=200):
        from System import Array, Byte
        from System.IO import MemoryStream
        stream = MemoryStream(Array[Byte](body))
        # WebView owns a COM reference while reading the response stream.
        response = self.window.native.webview.CoreWebView2.Environment.CreateWebResourceResponse(
            stream, status, 'OK' if status == 200 else 'Offline',
            'Content-Type: ' + mime + '\r\nCache-Control: no-store\r\nX-Hub-Desktop-Local: 1\r\n')
        return response

    def on_resource_requested(self, sender, args):
        if self._closed.is_set():
            return
        url, method = str(args.Request.Uri), str(args.Request.Method).upper()
        if not self.location.trusted(url):
            return
        path = urlsplit(url).path
        if path == self.location.path + '/logout' and method != 'GET':
            self.cache.clear_current()
        # Tag requests before they leave; logout must also invalidate responses
        # that were already in flight, not just later cache-writer threads.
        if hasattr(args.Request, 'Headers'):
            args.Request.Headers.SetHeader('X-Hub-Desktop-Cache-Epoch', str(self.cache.generation))
        login = path == self.location.path + '/login' and self._login_network_ready
        if method not in {'GET', 'HEAD', 'OPTIONS'} and self._offline and not login:
            args.Response = self._resource_response(b'{"error":"desktop_offline","message":"Not sent to server"}', 'application/json', 503)
            return
        if method != 'GET':
            return
        snapshot = self._offline_response
        if snapshot and canonical_url(url) == snapshot['url'] and str(args.ResourceContext) == 'Document':
            self._offline_response = None
            args.Response = self._resource_response(snapshot['html'].encode('utf-8'), 'text/html; charset=utf-8')
            return
        resource = self.assets.read(self.location, url)
        if resource:
            args.Response = self._resource_response(resource[0], resource[1])

    def on_resource_response(self, sender, args):
        if self._closed.is_set():
            return
        url = str(args.Request.Uri)
        if not self.location.trusted(url) or str(args.Request.Method).upper() != 'GET':
            return
        response = args.Response
        if int(response.StatusCode) != 200 or response.Headers.Contains('X-Hub-Desktop-Local'):
            return
        try:
            generation = int(str(args.Request.Headers.GetHeader('X-Hub-Desktop-Cache-Epoch')))
        except (ValueError, AttributeError):
            return
        if generation != self.cache.generation:
            return
        if urlsplit(url).path == self.location.path + '/login':
            self._login_network_ready = True
            self.cache.clear_current()
            return
        if not urlsplit(url).path.startswith(self.location.path + '/projects'):
            return
        content_type = str(response.Headers.GetHeader('Content-Type')) if response.Headers.Contains('Content-Type') else ''
        if 'text/html' not in content_type:
            return
        task = response.GetContentAsync()
        def capture():
            try:
                if self._closed.is_set():
                    return
                stream = task.GetAwaiter().GetResult()
                if stream is None:
                    return
                from System.IO import StreamReader
                reader = StreamReader(stream)
                source = str(reader.ReadToEnd())
                reader.Dispose()
                if not self._closed.is_set():
                    self.cache.put(url, source, generation=generation)
            except Exception:
                self.log.write('Workspace cache refresh skipped; remote navigation remains available.')
        threading.Thread(target=capture, daemon=True).start()

    def connect_native_events(self):
        if self._closed.is_set():
            return
        control = self.window.native.webview
        control.NavigationStarting += self.on_navigation
        control.NavigationCompleted += self.on_navigation_completed
        control.CoreWebView2InitializationCompleted += self.on_native_ready
        if control.CoreWebView2 is not None:
            self.on_native_ready()

    def _write_geometry_locked(self):
        if not self._geometry_dirty:
            return
        try:
            _atomic_json(self.state_dir / 'window.json', self._geometry)
            self._geometry_dirty = False
        except OSError:
            # Window position is optional. A full/locked state folder must not
            # break dragging, closing, or the actual conversation.
            try:
                self.log.write('Window position could not be saved; workspace remains available.')
            except OSError:
                pass

    def _flush_geometry(self, serial=None):
        with self._geometry_lock:
            if serial is not None and serial != self._geometry_serial:
                return
            self._geometry_serial += 1
            if self._geometry_timer:
                self._geometry_timer.cancel()
                self._geometry_timer = None
            self._write_geometry_locked()

    def save_geometry(self, *, defer=False, **changes):
        if self._closed.is_set():
            return
        with self._geometry_lock:
            if self._closed.is_set():
                return
            self._geometry.update(changes)
            self._geometry_dirty = True
            if defer:
                # Native move/resize bursts update memory immediately, but
                # coalesce disk writes off the UI callback into one short batch.
                if self._geometry_timer is None:
                    self._geometry_serial += 1
                    serial = self._geometry_serial
                    self._geometry_timer = threading.Timer(.25, lambda: self._flush_geometry(serial))
                    self._geometry_timer.daemon = True
                    self._geometry_timer.start()
                return
            self._geometry_serial += 1
            if self._geometry_timer:
                self._geometry_timer.cancel()
                self._geometry_timer = None
            self._write_geometry_locked()

    def on_resize(self, width, height):
        if not self._geometry.get('maximized') and width >= 900 and height >= 620:
            self.save_geometry(defer=True, width=int(width), height=int(height))

    def on_move(self, x, y):
        if not self._geometry.get('maximized'):
            self.save_geometry(defer=True, x=int(x), y=int(y))


def launch_desktop(config_path: Path | None = None, hub_override: str | None = None):
    """Launch/wake the foreground; never stop or duplicate the Bridge worker."""
    config_path = Path(config_path or default_config_path()).resolve()
    options = visible_gui_subprocess_kwargs()
    if getattr(sys, 'frozen', False):
        executable = Path(sys.executable).with_name('HubDesktop.exe')
        if not executable.is_file():
            raise FileNotFoundError('Hub 桌面组件尚未安装，请更新桌面程序。')
        command = [str(executable)]
        # HubDesktop is a distinct onefile bundle, not a child reusing Bridge's
        # extraction directory. Keep it alive when the launching Bridge exits.
        environment = os.environ.copy()
        environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
        options['env'] = environment
    else:
        command = [sys.executable, '-m', 'codex_bridge.desktop_window']
    command.extend(['--config', str(config_path)])
    if hub_override:
        command.extend(['--hub', HubLocation(hub_override).base])
    return subprocess.Popen(command, **options)


def request_desktop_close(config_path: Path | None = None, hub_override: str | None = None) -> bool:
    """Close only the matching foreground after the Bridge safely exits its work."""
    config = load_config(Path(config_path or default_config_path()))
    return DesktopInstance(HubLocation(hub_override or config.hub).key).request_close()


def run_desktop(config_path: Path, *, hub_override=None, webview_module=None):
    config = load_config(config_path)
    location = HubLocation(hub_override or config.hub)
    state_dir = desktop_state_dir(location)
    instance = DesktopInstance(location.key)
    if not instance.acquire():
        return 0
    host = None
    try:
        if webview_module is None:
            import webview as webview_module
        webview = webview_module
        from webview.menu import Menu, MenuAction
        host = DesktopHost(location, config.projects, state_dir)
        geometry = read_geometry(state_dir / 'window.json', webview.screens)
        host._geometry = geometry.copy()
        menu = [Menu('Hub', [MenuAction('上一页', lambda: host.window.run_js('history.back()')),
            MenuAction('返回工作区', host.navigate_home), MenuAction('重新连接', host.reconnect)])]
        if host.projects:
            menu.append(Menu('本机工程', [MenuAction(path.name, lambda slug=slug: host.open_project(slug))
                for slug, path in host.projects.items()]))
        webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = False
        webview.settings['ALLOW_DOWNLOADS'] = True
        webview.settings['IGNORE_SSL_ERRORS'] = False
        window = webview.create_window(TITLE, html=offline_page(), js_api=DesktopAPI(host),
            min_size=(900, 620), background_color='#151819', text_select=True, zoomable=True,
            menu=menu, **geometry)
        host.window = window
        window.events.before_show += host.connect_native_events
        window.events.loaded += host.on_loaded
        window.events.resized += host.on_resize
        window.events.moved += host.on_move
        window.events.closing += host.close_children
        window.events.closed += host.close_children
        window.events.maximized += lambda: host.save_geometry(maximized=True)
        window.events.restored += lambda: host.save_geometry(maximized=False)
        instance._watcher = threading.Thread(target=instance.watch, args=(host.bring_forward, window.destroy), daemon=True)
        instance._watcher.start()
        host.log.write('Desktop foreground starting; execution remains in Bridge.')
        webview.start(gui='edgechromium', private_mode=False, storage_path=str(state_dir / 'webview'), debug=False)
        host.log.write('Desktop foreground closed; Bridge execution was not stopped.')
        return 0
    finally:
        try:
            if host:
                host.close_children()
        finally:
            instance.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='BYBC Hub desktop workspace')
    parser.add_argument('--config', type=Path, default=default_config_path())
    parser.add_argument('--hub')
    arguments = parser.parse_args(argv)
    try:
        return run_desktop(arguments.config, hub_override=arguments.hub)
    except Exception as exc:
        # Keep details in a bounded local log. Never include config or tokens.
        log = PersistentLog(Path(os.environ.get('APPDATA') or Path.home()) / 'CodexHubDesktop' / 'launcher.log')
        log.write('Desktop launch failed: ' + type(exc).__name__)
        if sys.platform == 'win32':
            ctypes.windll.user32.MessageBoxW(None,
                'Hub 桌面窗口未能打开。请确认 Microsoft Edge WebView2 Runtime 已安装。后台工作不受此窗口影响。', TITLE, 0x10)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
