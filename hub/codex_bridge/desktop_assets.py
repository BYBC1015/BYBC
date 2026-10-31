"""Bounded desktop workspace snapshots and packaged, same-origin resources.

Only authenticated workspace HTML is retained. Login pages, cookies, request
headers, CSRF credentials and attachment bodies are never persisted here.
"""
from __future__ import annotations

import hashlib
import html
from html.parser import HTMLParser
import json
import math
import mimetypes
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import unquote, urlsplit, urlunsplit, urljoin, quote


class WorkspaceIdentity(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.user = None
        self.csrf = False
        self.release = ''
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if 'data-ws-shell' in values and values.get('data-user'):
            self.user = values['data-user'][:128]
            self.release = values.get('data-ws-release', '')[:64]
        if tag == 'input' and values.get('name') == '_csrf' and values.get('value'):
            self.csrf = True


def canonical_url(url):
    value = urlsplit(url)
    return urlunsplit((value.scheme, value.netloc, value.path, value.query, ''))


def scrub_credentials(source):
    # The application uses hidden _csrf inputs. Remove the whole value rather
    # than retaining a stale token in an offline document or disk snapshot.
    def scrub(match):
        tag = match.group()
        if re.search(r'\bname\s*=\s*[\"\']_csrf[\"\']', tag, re.I):
            return re.sub(r'\bvalue\s*=\s*([\"\']).*?\1', 'value=""', tag, flags=re.I | re.S)
        return tag
    return re.sub(r'<input\b[^>]*>', scrub, source, flags=re.I | re.S)


def offline_document(source):
    source = re.sub(r'<html\b', '<html data-hub-offline="true"', source, count=1, flags=re.I)
    marker = """<script>window.__HUB_DESKTOP_OFFLINE__=true;document.documentElement.dataset.hubOffline='true';</script>"""
    banner = """<aside id="hub-desktop-offline-banner" role="status" style="position:fixed;z-index:2147483000;top:8px;left:50%;transform:translateX(-50%);max-width:90vw;padding:9px 18px;border-radius:18px;background:#393424;color:#fff2ce;font:13px system-ui;box-shadow:0 3px 20px #0004">离线工作区 · 显示本机保存的内容，消息尚未发送到服务器</aside>"""
    source = re.sub(r'(<head\b[^>]*>)', lambda m: m.group() + marker, source, count=1, flags=re.I)
    if marker not in source:
        source = marker + source
    return re.sub(r'(<body\b[^>]*>)', lambda m: m.group() + banner, source, count=1, flags=re.I)


class WorkspaceCache:
    MAX_BYTES = 10 * 1024 * 1024
    MAX_PAGE_BYTES = 2 * 1024 * 1024
    MAX_PAGES = 20
    MAX_INDEX_BYTES = 256 * 1024

    def __init__(self, directory):
        self.directory = Path(directory)
        self.lock = threading.RLock()
        self.generation = 0
        self.latest_verified = None
        self.active_account = self._index()['account']

    def _index(self):
        try:
            path = self.directory / 'index.json'
            if path.stat().st_size > self.MAX_INDEX_BYTES:
                raise ValueError()
            data = json.loads(path.read_text(encoding='utf-8'))
            if (not isinstance(data, dict) or 'account' not in data
                    or not isinstance(data.get('pages'), dict)
                    or len(data['pages']) > self.MAX_PAGES):
                raise ValueError()
            account = data['account']
            if account is not None and (not isinstance(account, str) or not re.fullmatch(r'[a-f0-9]{24}', account)):
                raise ValueError()
            for key, page in data['pages'].items():
                if not re.fullmatch(r'[a-f0-9]{24}-[a-f0-9]{32}\.html', key) or not isinstance(page, dict):
                    raise ValueError()
                url, saved_account = page.get('url'), page.get('account')
                if (not isinstance(url, str) or len(url) > 8192
                        or not isinstance(saved_account, str) or saved_account != key[:24]
                        or type(page.get('bytes')) is not int or not 0 <= page['bytes'] <= self.MAX_PAGE_BYTES
                        or type(page.get('time')) not in (int, float) or not math.isfinite(page['time'])
                        or page['time'] < 0 or not isinstance(page.get('sha256'), str)
                        or not re.fullmatch(r'[a-f0-9]{64}', page['sha256'])
                        or key[25:-5] != hashlib.sha256(url.encode()).hexdigest()[:32]):
                    raise ValueError()
            return data
        except (OSError, ValueError, TypeError, UnicodeError):
            return {'account': None, 'pages': {}}

    def _write_index(self, data):
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.directory / 'index.tmp'
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        temporary.replace(self.directory / 'index.json')

    @staticmethod
    def account_key(user):
        return hashlib.sha256(user.encode()).hexdigest()[:24]

    def verify_response(self, url, source, *, generation=None):
        """Record network authentication independently of optional disk caching."""
        identity = WorkspaceIdentity(source)
        if not identity.user or not identity.csrf:
            return False
        url = canonical_url(url)
        account = self.account_key(identity.user)
        with self.lock:
            if generation is not None and generation != self.generation:
                return False
            if self.active_account and self.active_account != account:
                # Invalidate responses requested by the previous login session.
                self.generation += 1
            self.active_account = account
            self.latest_verified = {'account': account, 'user': identity.user, 'time': time.time(),
                                    'url': url, 'generation': self.generation}
        return True

    def put(self, url, source, *, generation=None):
        with self.lock:
            if not self.verify_response(url, source, generation=generation):
                return False
            if len(source.encode('utf-8')) > self.MAX_PAGE_BYTES:
                return False
            try:
                return self._persist_snapshot(url, source)
            except (OSError, UnicodeError):
                # Storage availability must never determine network permission.
                return False

    def _persist_snapshot(self, url, source):
        url = canonical_url(url)
        account = self.active_account
        key = account + '-' + hashlib.sha256(url.encode()).hexdigest()[:32] + '.html'
        body = scrub_credentials(source)
        data = self._index()
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / key
        temporary = target.with_suffix('.tmp')
        # Windows text-mode newline translation must not make the stored bytes
        # differ from the digest/length recorded for this exact document.
        temporary.write_bytes(body.encode('utf-8'))
        temporary.replace(target)
        now = time.time()
        data['account'] = account
        data['pages'][key] = {'account': account, 'url': url, 'time': now,
                              'bytes': len(body.encode('utf-8')),
                              'sha256': hashlib.sha256(body.encode('utf-8')).hexdigest()}
        pages = data['pages']
        ordered = sorted(pages, key=lambda name: pages[name]['time'])
        while len(pages) > self.MAX_PAGES or sum(item['bytes'] for item in pages.values()) > self.MAX_BYTES:
            oldest = ordered.pop(0)
            (self.directory / oldest).unlink(missing_ok=True)
            pages.pop(oldest)
        self._write_index(data)
        return True

    def get(self, url, *, latest=False):
        with self.lock:
            data = self._index()
            if not self.active_account or data['account'] != self.active_account:
                return None
            matches = [(key, page) for key, page in data['pages'].items()
                if page['account'] == data['account'] and (latest or page['url'] == canonical_url(url))]
            if not matches:
                return None
            key, page = max(matches, key=lambda item: item[1]['time'])
            if not re.fullmatch(r'[a-f0-9]{24}-[a-f0-9]{32}\.html', key):
                return None
            try:
                path = self.directory / key
                if path.stat().st_size > self.MAX_PAGE_BYTES * 2:
                    return None
                raw = path.read_bytes()
                if len(raw) != page['bytes'] or hashlib.sha256(raw).hexdigest() != page.get('sha256'):
                    # Older Windows builds wrote CRLF while indexing LF. Admit
                    # only that exact reversible representation when its length
                    # AND original digest agree; unrelated corruption stays out.
                    raw = raw.replace(b'\r\n', b'\n')
                    if len(raw) != page['bytes'] or hashlib.sha256(raw).hexdigest() != page.get('sha256'):
                        return None
                content = raw.decode('utf-8')
            except (OSError, UnicodeError):
                return None
            if hashlib.sha256(content.encode('utf-8')).hexdigest() != page.get('sha256'):
                return None
            identity = WorkspaceIdentity(content)
            if not identity.user or identity.csrf or self.account_key(identity.user) != data['account']:
                return None
            return {'url': page['url'], 'html': offline_document(content), 'source_html': content,
                    'generation': self.generation,
                    'account': data['account'], 'release': identity.release}

    def snapshot_current(self, snapshot):
        with self.lock:
            return (snapshot.get('generation') == self.generation
                    and bool(self.active_account)
                    and snapshot.get('account') == self.active_account)

    def clear_current(self):
        with self.lock:
            self.generation += 1
            self.latest_verified = None
            self.active_account = None
            data = self._index()
            for key, page in list(data['pages'].items()):
                if page['account'] == data['account']:
                    if re.fullmatch(r'[a-f0-9]{24}-[a-f0-9]{32}\.html', key):
                        try:
                            (self.directory / key).unlink(missing_ok=True)
                        except OSError:
                            pass
                    data['pages'].pop(key)
            data['account'] = None
            try:
                self._write_index(data)
            except OSError:
                # Runtime invalidation still applies when disk persistence fails.
                pass


class SnapshotLayout(WorkspaceIdentity):
    """Recognize the standard workspace, and extract safe text for older formats."""
    REQUIRED = {'data-ws-shell', 'data-ws-dispatch', 'data-ws-input', 'data-ws-send',
        'data-ws-dispatch-error', 'data-ws-scroll', 'data-ws-new-messages', 'data-ws-sidebar',
        'data-ws-inspector', 'data-ws-task-panels', 'data-ws-backdrop', 'data-ws-recipient-picker',
        'data-ws-attachment-input', 'data-ws-attach', 'data-ws-attachments',
        'data-ws-attachment-error', 'data-ws-upload-progress', 'data-ws-pending',
        'data-ws-receipt-check', 'data-ws-queue-cancel', 'data-ws-live'}
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self, source):
        self.markers, self.fields = set(), set()
        self.shell = {}
        self.title_parts, self.records = [], []
        self._title = False
        self._head = False
        self.head_seen = False
        self._shell = False
        self.body_parts = []
        self.body_scripts = False
        self._record = None
        self._depth = 0
        self._skip = None
        super().__init__(source)

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        values = dict(attrs)
        self.markers.update(key for key in values if key.startswith('data-ws-'))
        if 'data-ws-shell' in values:
            self.shell = values
            self._shell = True
        if values.get('name'):
            self.fields.add(values['name'])
        if tag == 'title':
            self._title = True
        if tag == 'head':
            self._head = True
            self.head_seen = True
        if tag == 'script' and not self._head:
            self.body_scripts = True
        if tag in {'script', 'style'}:
            self._skip = tag
        if self._record is not None and tag not in self.VOID:
            self._depth += 1
        if self._record is None and tag == 'article' and 'ws-message' in values.get('class', '').split():
            self._record = {'user': 'ws-message-user' in values.get('class', '').split(), 'parts': []}
            self._depth = 1
        if self._record is not None and tag == 'img' and values.get('alt'):
            self._record['parts'].append('附件：' + values['alt'])

    def handle_data(self, data):
        if self._title:
            self.title_parts.append(data)
        if self._record is not None and not self._skip and data.strip():
            self._record['parts'].append(data.strip())
        if self._shell and not self._skip and data.strip():
            self.body_parts.append(data.strip())

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if tag == self._skip:
            self._skip = None
        if tag == 'title':
            self._title = False
        if tag == 'head':
            self._head = False
        if tag == 'main':
            self._shell = False
        if self._record is not None:
            self._depth -= 1
            if self._depth <= 0:
                self.records.append({'user': self._record['user'], 'text': '\n'.join(self._record['parts'])})
                self._record = None

    @property
    def compatible(self):
        return (self.head_seen and not self._head and not self.body_scripts and self.REQUIRED <= self.markers and {'request_key', 'title', 'description', '_csrf'} <= self.fields
                and all(self.shell.get(key) for key in ('data-user', 'data-project', 'data-conversation', 'data-workspace-url')))


class PackagedAssets:
    def __init__(self, root=None):
        bundle = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
        self.root = Path(root or bundle / 'codex_hub' / 'static').resolve()
        try:
            document = json.loads((self.root / 'desktop-assets.json').read_text(encoding='utf-8'))
            # Source runs must not inherit a manifest generated for an older
            # release. Frozen bundles keep their build-time manifest immutable.
            if root is None and not getattr(sys, 'frozen', False):
                document = build_asset_manifest(self.root, self.root.parent / 'templates')
            self.manifest = document['assets']
            self.workspace = document.get('workspace', {})
            if not isinstance(self.manifest, dict) or not isinstance(self.workspace, dict):
                raise ValueError()
        except (OSError, ValueError, TypeError, KeyError):
            self.manifest = {}
            self.workspace = {}
            if not getattr(sys, 'frozen', False):
                try:
                    document = build_asset_manifest(self.root, self.root.parent / 'templates')
                    self.manifest, self.workspace = document['assets'], document.get('workspace', {})
                except (OSError, ValueError):
                    pass

    def read(self, location, url):
        if not location.trusted(url):
            return None
        path = unquote(urlsplit(url).path)
        prefix = location.path + '/static/'
        if not path.startswith(prefix):
            return None
        relative = path[len(prefix):]
        if not relative or '\\' in relative or ':' in relative or '..' in relative.split('/'):
            return None
        declared = self.manifest.get(relative)
        if not isinstance(declared, dict) or not isinstance(declared.get('queries'), list):
            return None
        query = urlsplit(url).query
        digest = declared.get('sha256', '')
        # A manual v=20261029 value is not a content identity. Serving bundled
        # JS/CSS for that URL can silently override a newer server release.
        immutable_dependency = (not query and '' in declared['queries']
            and Path(relative).suffix.lower() in {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.svg', '.woff', '.woff2', '.ttf'})
        if not immutable_dependency and (not re.fullmatch(r'[a-f0-9]{64}', digest)
                or query != 'v=' + digest[:16]):
            return None
        try:
            target = (self.root / relative).resolve()
            if not target.is_relative_to(self.root) or not target.is_file() or target.stat().st_size > 16 * 1024 * 1024:
                return None
            content = target.read_bytes()
        except (OSError, ValueError):
            # Optional packaged acceleration must not prevent network loading
            # during an update, transient file lock or incomplete installation.
            return None
        mime = mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
        if target.suffix.lower() == '.js':
            mime = 'application/javascript'
        if hashlib.sha256(content).hexdigest() != declared.get('sha256'):
            return None
        return content, mime

    def asset_url(self, location, relative):
        entry = self.manifest.get(relative, {})
        digest = entry.get('sha256', '')
        if not re.fullmatch(r'[a-f0-9]{64}', digest):
            return ''
        url = location.base + '/static/' + quote(relative, safe='/') + '?v=' + digest[:16]
        return url if self.read(location, url) else ''

    def prepare_snapshot(self, location, snapshot):
        """Upgrade display resources only; never rewrite the persisted record.

        Known workspace structure retains its original scope, fields and message
        content. Unknown/newer structure is presented with the current visual
        shell as read-only records, without executing incompatible old scripts.
        """
        source = snapshot.get('source_html') or snapshot['html']
        layout = SnapshotLayout(source)
        current = str(self.workspace.get('release', ''))
        styles = [self.asset_url(location, name) for name in self.workspace.get('styles', [])]
        scripts = [self.asset_url(location, name) for name in self.workspace.get('scripts', [])]
        complete = bool(styles and scripts and all(styles) and all(scripts))
        newer = self._release_parts(layout.release) > self._release_parts(current)
        compatible = layout.compatible and complete and not newer
        title = ''.join(layout.title_parts).strip() or '本机工作记录 · Codex Hub'
        head = '<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="color-scheme" content="dark">'
        head += '<meta name="hub-ui-release" content="' + html.escape(current, quote=True) + '"><title>' + html.escape(title) + '</title>'
        head += ''.join('<link rel="stylesheet" href="' + html.escape(url, quote=True) + '">' for url in styles if url)
        if compatible:
            head += ''.join('<script src="' + html.escape(url, quote=True) + '" defer></script>' for url in scripts)
        head += '</head>'
        if compatible:
            # Replace the entire old head, not just one stylesheet. A retained
            # legacy script or unversioned dependency could recreate the old UI.
            body = re.sub(r'<head\b[^>]*>.*?</head\s*>', lambda _: head, source, count=1, flags=re.I | re.S)
            def shell_tag(match):
                tag = re.sub(r'\sdata-ws-release\s*=\s*([\"\']).*?\1', '', match.group(), flags=re.I | re.S)
                return tag[:-1] + ' data-ws-release="' + html.escape(current, quote=True) + '" data-hub-cache-upgraded="true">'
            body = re.sub(r'<main\b[^>]*\bdata-ws-shell\b[^>]*>', shell_tag, body, count=1, flags=re.I | re.S)
            # Body image references, including the owl, need the same verified
            # bundle identity while offline. Never redirect external resources.
            def reference(match):
                target = urlsplit(urljoin(snapshot['url'], html.unescape(match.group(2))))
                if not location.trusted(urlunsplit(target)) or not unquote(target.path).startswith(location.path + '/static/'):
                    return match.group()
                relative = unquote(target.path)[len(location.path + '/static/'):]
                replacement = self.asset_url(location, relative)
                return match.group(1) + '="' + html.escape(replacement, quote=True) + '"' if replacement else match.group()
            body = re.sub(r'\b(src|href)\s*=\s*[\"]([^\"]*)[\"]', reference, body, flags=re.I)
        else:
            body = self._read_only_document(location, layout, head, current, title)
        result = dict(snapshot)
        result.update(html=offline_document(body), release=current, upgraded=compatible, read_only=not compatible)
        return result

    @staticmethod
    def _release_parts(value):
        return tuple(map(int, value.split('.'))) if re.fullmatch(r'\d+\.\d+\.\d+', value or '') else (0, 0, 0)

    def _read_only_document(self, location, layout, head, current, title):
        logo = self.asset_url(location, 'assets/codex-owl-logo-lite-20260708.png')
        brand = '<img width="42" height="42" alt="Hub" src="' + html.escape(logo, quote=True) + '">' if logo else '<strong>Hub</strong>'
        records = ''.join('<article class="ws-message' + (' ws-message-user' if item['user'] else '')
            + '"><div class="ws-message-content"><div class="ws-message-meta"><strong>'
            + ('你' if item['user'] else '工作记录') + '</strong></div><div class="ws-message-prose'
            + (' ws-user-bubble' if item['user'] else '') + '"><pre>' + html.escape(item['text'])
            + '</pre></div></div></article>' for item in layout.records)
        if not records:
            records = ('<article class="ws-message"><div class="ws-message-content"><pre>'
                + html.escape('\n'.join(layout.body_parts)) + '</pre></div></article>') if layout.body_parts else '<p>原始记录已保留在本机缓存中；联网后将恢复完整工作区。</p>'
        user = html.escape(layout.user or '', quote=True)
        # No previous script, form, inline handler, CSRF value, or attachment
        # body is imported into the read-only display. Draft/outbox storage is
        # untouched. Recovery performs only GET and asks the native account guard.
        controls = """<script>(()=>{const retry=document.querySelector('[data-cache-retry]');retry.addEventListener('click',()=>window.pywebview?.api.retry_connection());let running=false;async function probe(){if(running||document.hidden||!window.pywebview?.api)return;running=true;const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),8000);try{const response=await fetch(location.href,{credentials:'same-origin',cache:'no-store',signal:controller.signal});if(!response.ok)return;const doc=new DOMParser().parseFromString(await response.text(),'text/html');const shell=doc.querySelector('[data-ws-shell]');if(!shell||!doc.querySelector('input[name="_csrf"]')?.value)return;const own=document.querySelector('[data-ws-shell]').dataset.user;if(shell.dataset.user!==own){location.reload();return;}const result=await window.pywebview.api.workspace_online(own);if(result?.online)location.reload();}catch{}finally{clearTimeout(timer);running=false;}}window.addEventListener('online',probe);setInterval(probe,15000);})();</script>"""
        style = """<style>body.workspace-screen{margin:0;background:#101112;color:#eeedea;font:16px/1.7 "Noto Sans SC","Microsoft YaHei UI",sans-serif}.ws-cache-layout{display:grid;grid-template-columns:220px minmax(0,1fr);gap:12px;padding:12px;height:100dvh;box-sizing:border-box}.ws-cache-layout .ws-sidebar{display:flex;flex-direction:column;gap:22px;background:#17191b;border-radius:24px;padding:22px 16px}.ws-cache-layout .ws-center{display:grid;min-width:0;min-height:0;grid-template-rows:auto minmax(0,1fr) auto;background:#1d1f21;border-radius:24px}.ws-cache-layout .ws-chat-head{padding:22px}.ws-cache-layout .ws-messages{overflow:auto;padding:24px}.ws-cache-layout pre{font:inherit;white-space:pre-wrap;overflow-wrap:anywhere;margin:0}.ws-cache-footer{padding:20px 26px;border-top:1px solid #ffffff12;color:#b2b5b7;font-size:13px}.ws-cache-layout button{font:inherit;background:#d4c2aa;color:#17191b;border:0;border-radius:16px;padding:10px 18px;cursor:pointer}.ws-cache-layout .ws-cache-note{color:#b2b5b7;font-size:13px}@media(max-width:700px){.ws-cache-layout{grid-template-columns:1fr}.ws-cache-layout .ws-sidebar{display:none}}</style>"""
        return '<!doctype html><html lang="zh-CN">' + head.replace('</head>', style + '</head>') + '<body class="workspace-screen hub-detail-screen"><main class="ws-cache-layout" data-ws-shell data-user="' + user + '" data-ws-release="' + html.escape(current, quote=True) + '" data-hub-cache-readonly="true"><aside class="ws-sidebar"><div>' + brand + ' <strong>Hub 工作区</strong></div><p class="ws-cache-note">网络连接暂时中断。这里显示本机保存的对话记录。</p></aside><section class="ws-center"><header class="ws-chat-head"><div class="ws-chat-title"><h1>' + html.escape(title) + '</h1><p>离线记录 · 当前界面</p></div></header><div class="ws-messages"><div class="ws-message-feed">' + records + '</div></div><footer class="ws-cache-footer"><p>旧版缓存暂以只读方式展示，文字草稿和待发送附件仍保存在原工作区；这里不会重复派发任务。</p><button type="button" data-cache-retry>重新连接</button></footer></section></main>' + controls + '</body></html>'


def build_asset_manifest(static_root, templates_root):
    assets = {}
    root = Path(static_root).resolve()
    # Match explicit app_path static references and their literal cache version.
    pattern = re.compile(r"(app_path|asset_path)\(['\"]/static/([^'\"]+)['\"]\)\s*}}(?:\?([^\"'<>\s]+))?")
    workspace = {'styles': [], 'scripts': []}
    for template in Path(templates_root).rglob('*.html'):
        for helper, relative, query in pattern.findall(template.read_text(encoding='utf-8')):
            path = (root / relative).resolve()
            if path.is_relative_to(root) and path.is_file():
                entry = assets.setdefault(relative, {'queries': [], 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
                if helper == 'asset_path':
                    query = 'v=' + entry['sha256'][:16]
                if query not in entry['queries']:
                    entry['queries'].append(query)
                if template.name == 'project_workspace.html':
                    group = 'styles' if relative.endswith('.css') else 'scripts' if relative.endswith('.js') else None
                    if group and relative not in workspace[group]:
                        workspace[group].append(relative)
    # CSS dependencies are relative to their own stylesheet, not the page.
    pending = [name for name in assets if name.endswith('.css')]
    scanned = set()
    while pending:
        stylesheet = pending.pop()
        if stylesheet in scanned:
            continue
        scanned.add(stylesheet)
        for reference in re.findall(r'url\(\s*[\"\']?([^\"\')\s]+)', (root / stylesheet).read_text(encoding='utf-8')):
            url = urlsplit(urljoin('https://local.invalid/static/' + stylesheet, reference))
            if url.netloc != 'local.invalid' or not url.path.startswith('/static/'):
                continue
            relative = unquote(url.path[len('/static/'):])
            path = (root / relative).resolve()
            if path.is_relative_to(root) and path.is_file():
                entry = assets.setdefault(relative, {'queries': [], 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
                if url.query not in entry['queries']:
                    entry['queries'].append(url.query)
                if relative.endswith('.css'):
                    pending.append(relative)
    from codex_hub import __version__ as ui_release
    workspace['release'] = ui_release
    return {'assets': assets, 'workspace': workspace}


def write_asset_manifest(static_root, templates_root):
    manifest = build_asset_manifest(static_root, templates_root)
    (Path(static_root) / 'desktop-assets.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest
