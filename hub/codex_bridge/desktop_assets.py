"""Bounded desktop workspace snapshots and packaged, same-origin resources.

Only authenticated workspace HTML is retained. Login pages, cookies, request
headers, CSRF credentials and attachment bodies are never persisted here.
"""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import json
import mimetypes
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import unquote, urlsplit, urlunsplit, urljoin


class WorkspaceIdentity(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.user = None
        self.csrf = False
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if 'data-ws-shell' in values and values.get('data-user'):
            self.user = values['data-user'][:128]
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

    def __init__(self, directory):
        self.directory = Path(directory)
        self.lock = threading.RLock()
        self.generation = 0
        self.latest_verified = None
        self.active_account = self._index()['account']

    def _index(self):
        try:
            data = json.loads((self.directory / 'index.json').read_text(encoding='utf-8'))
            if not isinstance(data.get('pages'), dict):
                raise ValueError()
            if any(not re.fullmatch(r'[a-f0-9]{24}-[a-f0-9]{32}\.html', key) or not isinstance(page, dict)
                   for key, page in data['pages'].items()):
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
        temporary.write_text(body, encoding='utf-8')
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
                content = (self.directory / key).read_text(encoding='utf-8')
            except (OSError, UnicodeError):
                return None
            if hashlib.sha256(content.encode('utf-8')).hexdigest() != page.get('sha256'):
                return None
            identity = WorkspaceIdentity(content)
            if not identity.user or identity.csrf or self.account_key(identity.user) != data['account']:
                return None
            return {'url': page['url'], 'html': offline_document(content), 'account': data['account']}

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


class PackagedAssets:
    def __init__(self, root=None):
        bundle = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
        self.root = Path(root or bundle / 'codex_hub' / 'static').resolve()
        try:
            self.manifest = json.loads((self.root / 'desktop-assets.json').read_text(encoding='utf-8'))['assets']
        except (OSError, ValueError, KeyError):
            self.manifest = build_asset_manifest(self.root, self.root.parent / 'templates')['assets'] if not getattr(sys, 'frozen', False) else {}

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
        if not declared or urlsplit(url).query not in declared.get('queries', []):
            return None
        target = (self.root / relative).resolve()
        if not target.is_relative_to(self.root) or not target.is_file() or target.stat().st_size > 16 * 1024 * 1024:
            return None
        mime = mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
        if target.suffix.lower() == '.js':
            mime = 'application/javascript'
        content = target.read_bytes()
        if hashlib.sha256(content).hexdigest() != declared.get('sha256'):
            return None
        return content, mime


def build_asset_manifest(static_root, templates_root):
    assets = {}
    root = Path(static_root).resolve()
    # Match explicit app_path static references and their literal cache version.
    pattern = re.compile(r"app_path\(['\"]/static/([^'\"]+)['\"]\)\s*}}(?:\?([^\"'<>\s]+))?")
    for template in Path(templates_root).rglob('*.html'):
        for relative, query in pattern.findall(template.read_text(encoding='utf-8')):
            path = (root / relative).resolve()
            if path.is_relative_to(root) and path.is_file():
                entry = assets.setdefault(relative, {'queries': [], 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
                if query not in entry['queries']:
                    entry['queries'].append(query)
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
    return {'assets': assets}


def write_asset_manifest(static_root, templates_root):
    manifest = build_asset_manifest(static_root, templates_root)
    (Path(static_root) / 'desktop-assets.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest
