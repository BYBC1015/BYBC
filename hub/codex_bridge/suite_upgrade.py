"""Local, fingerprint-based discovery and upgrade; never log credential contents."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import uuid
import zipfile

from . import __version__
from .config import BridgeConfig, default_config_path, token_is_configured

MARKER = '.hub-suite-install.json'
REGKEY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\CodexHubSuite'
VIDEO_REGKEY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\{B1D65F16-77CA-4C0E-9272-AF969AEF20B9}_is1'
TITLES = {'suite':'Bridge 统一安装版','bridge':'独立 Bridge','kling':'可灵批处理','music':'节拍工坊','video':'视频末帧延长'}


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def safe_child(root: Path, relative: str) -> Path:
    root = root.absolute()
    path = root / relative
    if Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('旧版清单包含无效路径，已停止清理。')
    resolved = path.resolve()
    if resolved == root.resolve() or not resolved.is_relative_to(root.resolve()):
        raise ValueError('旧版清单越出安装目录，已停止清理。')
    for part in [path, *path.parents]:
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('发现目录链接，已保留原文件。')
        if part == root: break
    return path


def load_catalog():
    catalog = json.loads((Path(__file__).parent / 'assets/suite-legacy.json').read_text(encoding='utf-8'))
    # The frozen installer's own payload also authenticates same-version reinstalls.
    archive = Path(getattr(sys, '_MEIPASS', '')) / 'payload.zip'
    if archive.is_file():
        with zipfile.ZipFile(archive) as z:
            body = z.read('bundle-manifest.json')
        manifest = json.loads(body)
        catalog['manifests'].append(hashlib.sha256(body).hexdigest())
        for item in manifest['files']:
            if item['path'] == 'CodexBridge.exe':
                catalog['entries'][item['sha256']] = {'kind':'bridge','version':manifest['version']}
    return catalog


@dataclass
class Candidate:
    root: Path
    entry: Path
    kind: str
    version: str
    owned: dict[str, str] = field(default_factory=dict)
    safe: bool = True
    reason: str = ''

    def summary(self):
        return {'name':TITLES[self.kind], 'version':self.version, 'path':str(self.entry),
                'can_clean':self.safe, 'reason':self.reason}


@dataclass
class Discovery:
    candidates: list[Candidate] = field(default_factory=list)
    config_paths: list[Path] = field(default_factory=list)
    links: list[dict] = field(default_factory=list, repr=False)


def identify(path: Path, catalog: dict) -> Candidate | None:
    path = path.absolute()
    root = path if path.is_dir() else path.parent
    entries = [path] if path.is_file() else [root / name for name in ('CodexBridge.exe','可灵批处理.exe','BYBC视频末帧延长工具.exe','package.json')]
    for entry in entries:
        if not entry.is_file(): continue
        try:
            safe_child(root, entry.name)
            checksum = digest(entry)
        except (OSError, ValueError):
            continue
        known = catalog['entries'].get(checksum)
        if known is None:
            if entry.suffix.lower() == '.exe':
                kind = 'bridge' if entry.name.startswith('CodexBridge') else ('kling' if entry.name == '可灵批处理.exe' else 'video')
                return Candidate(root, entry, kind, '未识别', safe=False, reason='文件不在已验证版本清单内，保留原文件')
            continue
        kind, version = known['kind'], known['version']
        if kind == 'bridge' and re.fullmatch(r'\d+(\.\d+)+', version) and tuple(map(int, version.split('.'))) > tuple(map(int, __version__.split('.'))):
            return Candidate(root, entry, kind, version, safe=False, reason='版本高于当前安装器，保留原文件')
        if kind == 'bridge' and (root / 'bundle-manifest.json').exists():
            try:
                manifest_path = safe_child(root, 'bundle-manifest.json')
                checksum_manifest = digest(manifest_path)
                if checksum_manifest not in catalog['manifests']:
                    return Candidate(root, entry, 'suite', version, safe=False, reason='旧版安装清单无法验证，保留原目录')
                manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
                owned = {item['path']:item['sha256'] for item in manifest['files']}
                for relative in owned: safe_child(root, relative)
                owned['bundle-manifest.json'] = checksum_manifest
                return Candidate(root, entry, 'suite', manifest['version'], owned)
            except (OSError, ValueError, KeyError):
                return Candidate(root, entry, 'suite', version, safe=False, reason='旧版安装清单无效，保留原目录')
        if kind == 'bridge': return Candidate(root, entry, kind, version, {entry.name:checksum})
        owned = {}
        for relative, hashes in catalog['files'][kind].items():
            try:
                file = safe_child(root, relative)
                if file.is_file() and digest(file) in hashes:
                    owned[relative] = digest(file)
            except (OSError, ValueError):
                continue
        return Candidate(root, entry, kind, version, owned)
    return None


def windows_locations():
    """Read only current-user shell folders and known product registrations."""
    import pythoncom
    import win32com.client
    import winreg
    roots, links = [], []
    for key_name in (REGKEY, VIDEO_REGKEY):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name) as key:
                value = winreg.QueryValueEx(key, 'InstallLocation')[0]
            if value: roots.append(Path(value))
        except OSError: pass
    initialized = False
    link = shell = None
    try:
        try:
            pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
            initialized = True
        except pythoncom.com_error as exc:
            if exc.hresult != -2147417850: raise
        shell = win32com.client.Dispatch('WScript.Shell')
        folders = [Path(shell.SpecialFolders(name)) for name in ('Desktop','Programs')]
        for folder in folders:
            for path in folder.rglob('*.lnk'):
                if not re.search(r'Codex.*Bridge|Codex Hub|可灵|BYBC.*视频|节拍工坊', path.name, re.I): continue
                link = shell.CreateShortcut(str(path))
                target, args = link.TargetPath, link.Arguments
                links.append({'path':str(path),'target':target,'args':args})
                if target and Path(target).name.lower().endswith('.exe') and not Path(target).name.lower().startswith('powershell'):
                    roots.append(Path(target))
        shell = None
    finally:
        link = shell = None
        if initialized: pythoncom.CoUninitialize()
    # The redirected Downloads known folder is stored here on Windows.
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as key:
            downloads = Path(os.path.expandvars(winreg.QueryValueEx(key, '{374DE290-123F-4565-9164-39C4925E467B}')[0]))
    except OSError: downloads = Path.home() / 'Downloads'
    roots.extend(downloads.glob('可灵批处理_v*'))
    import psutil
    for process in psutil.process_iter(['name','exe','cmdline']):
        try:
            name = process.info.get('name') or ''
            exe = process.info.get('exe')
            if exe and (name.startswith('CodexBridge') or name in {'可灵批处理.exe','BYBC视频末帧延长工具.exe'}):
                roots.append(Path(exe))
                args = process.info.get('cmdline') or []
                if '--config' in args and args.index('--config') + 1 < len(args):
                    config = args[args.index('--config') + 1]
                    links.append({'path':'','target':exe,'args':'--config "' + config + '"'})
        except (psutil.AccessDenied, psutil.NoSuchProcess): pass
    from .startup import StartupManager
    startup = StartupManager()
    for path in (startup.command_path, startup.legacy_command_path):
        try:
            body = path.read_bytes()
            value = body.decode('utf-16' if body.startswith((b'\xff\xfe',b'\xfe\xff')) else 'utf-8-sig').replace('""','"')
            for match in re.finditer(r'"([^"\r\n]*CodexBridge[^"\r\n]*\.exe)"', value, re.I):
                roots.append(Path(match.group(1)))
        except (OSError, UnicodeError): pass
    return roots, links


def discover(destination: Path, extra_roots=(), *, system=True, catalog=None):
    catalog = catalog or load_catalog()
    result = Discovery(config_paths=[default_config_path()])
    roots = [destination, *extra_roots]
    if system:
        locations, result.links = windows_locations()
        roots += locations
        programs = Path(os.environ['LOCALAPPDATA']) / 'Programs'
        roots += [programs / 'CodexHub', programs / 'BYBC视频末帧延长工具']
        for base in (Path(os.environ.get('ProgramFiles','C:/Program Files')), Path('D:/Program Files')):
            roots.extend(base.glob('可灵批处理_v*'))
        roots.append(Path(os.environ['PROGRAMDATA']) / 'Blackmagic Design/DaVinci Resolve/Support/Workflow Integration Plugins/com.local.beatworkshop')
        result.config_paths.append(Path.home() / '.codex-bridge/config.json')
    for suffix in ('.previous-*','.failed-*','.staging-*'):
        roots += list(destination.parent.glob(destination.name + suffix))
    seen = set()
    for root in roots:
        if not root.exists(): continue
        candidate = identify(root, catalog)
        if candidate is None or str(candidate.entry.resolve()).casefold() in seen: continue
        seen.add(str(candidate.entry.resolve()).casefold())
        result.candidates.append(candidate)
        if candidate.safe and candidate.kind in {'bridge','suite'}:
            result.config_paths += [candidate.root / 'config.json', candidate.root / 'codex-bridge.json']
        # Task shortcuts are data. Keep their batch arguments when retargeting.
        if candidate.safe and candidate.kind == 'kling':
            result.links += read_task_links(candidate.root)
    for link in result.links:
        if any(c.safe and str(c.entry).casefold() == link['target'].casefold() and c.kind in {'bridge','suite'} for c in result.candidates):
            match = re.search(r'--config(?:=|\s+)(?:"([^"]+)"|(\S+))', link['args'])
            if match:
                path = Path(match.group(1) or match.group(2))
                if path.is_absolute(): result.config_paths.append(path)
    result.config_paths = list(dict.fromkeys(p.absolute() for p in result.config_paths if p.is_file()))
    return result


def read_task_links(root):
    import pythoncom
    import win32com.client
    initialized = False
    link = shell = None
    try:
        try:
            pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
            initialized = True
        except pythoncom.com_error as exc:
            if exc.hresult != -2147417850: raise
        shell = win32com.client.Dispatch('WScript.Shell')
        result = []
        for path in root.glob('*.lnk'):
            link = shell.CreateShortcut(str(path))
            result.append({'path':str(path),'target':link.TargetPath,'args':link.Arguments})
        shell = None
        return result
    finally:
        link = shell = None
        if initialized: pythoncom.CoUninitialize()


def read_config(path):
    try:
        if path.stat().st_size > 1024 * 1024: raise ValueError()
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(data, dict) or not isinstance(data.get('hub'), str): raise ValueError()
        if 'token' in data and not isinstance(data['token'], str): raise ValueError()
        if not isinstance(data.get('bindings', []), list): raise ValueError()
        for binding in data.get('bindings', []):
            if not isinstance(binding, dict) or not isinstance(binding.get('token', ''), str): raise ValueError()
        BridgeConfig.from_mapping(data)
        return data
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError('旧配置无法读取或格式无效；原文件已保留。') from None


def credential_count(data):
    return sum(token_is_configured(token) for token in [data.get('token',''), *[b.get('token','') for b in data.get('bindings',[])]])


@dataclass(repr=False)
class ConfigPlan:
    destination: Path
    source: Path | None
    status: str
    count: int = 0
    original: bytes | None = field(default=None, repr=False)
    document: dict | None = field(default=None, repr=False)

    def summary(self):
        return {'status':self.status, 'credentials':self.count, 'source':str(self.source) if self.source else None}


def config_plan(paths, *, inherit=True, selected=None):
    destination = default_config_path()
    original = destination.read_bytes() if destination.exists() else None
    current = read_config(destination) if original is not None else None
    if current and credential_count(current):
        return ConfigPlan(destination, destination, '沿用已有 Token 和配置', credential_count(current), original)
    if not inherit: return ConfigPlan(destination, None, '未启用配置继承', original=original)
    if selected and not Path(selected).is_file():
        raise ValueError('所选旧配置文件不存在，请重新选择。')
    sources = []
    invalid = False
    for path in ([Path(selected)] if selected else paths):
        if path == destination or not path.is_file(): continue
        try:
            data = read_config(path)
            if credential_count(data): sources.append((path,data))
        except ValueError:
            if selected: raise
            invalid = True
    signatures = {json.dumps([d['hub'], d.get('token',''), d.get('bindings',[])], sort_keys=True, ensure_ascii=False) for _,d in sources}
    if len(signatures) > 1:
        raise ValueError('发现多个不同的旧配置。请点击“选择旧配置”，指定要继承的配置文件。')
    if not sources:
        if invalid: raise ValueError('发现旧配置但无法验证，原文件已保留；请修复或选择有效的旧配置。')
        return ConfigPlan(destination, None, '未发现可继承 Token，首次使用请配置', original=original)
    source, data = sources[0]
    document = copy.deepcopy(data)
    # Relative file paths remain tied to the old config's directory, never to
    # the new executable's working directory. Original state/history stay put.
    fields = ('prompt_dir','state_path','project_root','codex_exe_path','codex_cli_path','codex_exec_output_dir','hermes_cli_path','hermes_output_dir','vpn_exe_path')
    for obj in [document, *document.get('bindings',[])]:
        for key in fields:
            value = obj.get(key)
            if isinstance(value,str) and value and not Path(value).is_absolute(): obj[key] = str((source.parent / value).absolute())
    for key,value in document.get('projects',{}).items():
        if value and not Path(value).is_absolute(): document['projects'][key] = str((source.parent / value).absolute())
    return ConfigPlan(destination, source, '将继承旧版 Token 和配置', credential_count(data), original, document)


def apply_config(plan):
    current = plan.destination.read_bytes() if plan.destination.exists() else None
    if current != plan.original: raise RuntimeError('配置在安装期间发生变化，请重新检测后再安装。')
    if plan.document is None: return plan.summary()
    plan.destination.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(plan.document, ensure_ascii=False, indent=2).encode('utf-8')
    pending = plan.destination.with_name('config.upgrade-' + uuid.uuid4().hex + '.tmp')
    try:
        with pending.open('xb') as stream: stream.write(body)
        read_config(pending)
        if plan.original is not None:
            backup = plan.destination.with_name('config.before-upgrade-' + uuid.uuid4().hex + '.json')
            with backup.open('xb') as stream: stream.write(plan.original)
        os.replace(pending, plan.destination)
    finally:
        if pending.exists(): pending.unlink()
    if plan.destination.read_bytes() != body: raise RuntimeError('配置迁移校验失败，旧版尚未清理。')
    return {**plan.summary(), 'status':'已继承旧版 Token 和配置'}


def cleanup(candidate, *, protected_root: Path, check_idle):
    """Only authenticated unchanged program files; never recursive deletion."""
    result = {**candidate.summary(), 'removed_files':0, 'remaining_files':0}
    if not candidate.safe:
        result['status'] = '保留：' + candidate.reason
        return result
    if candidate.root.resolve() == protected_root.resolve() or candidate.entry.resolve().is_relative_to(protected_root.resolve()):
        result['status'] = '保留当前安装'
        return result
    try:
        check_idle(candidate.entry if candidate.kind == 'bridge' else candidate.root)
        targets = [safe_child(candidate.root, relative) for relative in candidate.owned]
    except (OSError, ValueError, RuntimeError):
        result['status'] = '占用中或路径异常，未清理；关闭工具后可重试'
        return result
    expected_entry = candidate.owned.get(candidate.entry.name)
    if candidate.entry.is_file() and expected_entry and digest(candidate.entry) != expected_entry:
        result.update(status='旧程序在检测后发生变化，已全部保留', remaining_files=1)
        return result
    parents = set()
    for relative, path in zip(candidate.owned, targets):
        try:
            path = safe_child(candidate.root, relative)
            if path.is_file():
                if digest(path) != candidate.owned[relative]:
                    result['remaining_files'] += 1
                    continue
                path.unlink()
                parents.add(path.parent)
                result['removed_files'] += 1
        except (OSError, ValueError):
            result['remaining_files'] += 1
    if candidate.kind == 'suite' and result['remaining_files'] == 0:
        try:
            marker = safe_child(candidate.root, MARKER)
            if marker.is_file():
                state = json.loads(marker.read_text(encoding='utf-8'))
                if state.get('version') == candidate.version: marker.unlink()
        except (OSError, ValueError): pass
    for folder in sorted(parents | {candidate.root}, key=lambda p:len(p.parts), reverse=True):
        while folder.is_relative_to(candidate.root):
            try: folder.rmdir()
            except OSError: break
            if folder == candidate.root: break
            folder = folder.parent
    result['status'] = '已清理旧程序；个人文件保留' if not candidate.entry.exists() else '部分文件已保留，请关闭程序后重试'
    return result


def carry_user_files(source, stage, candidate):
    """Keep install-local user files at the same path after the directory swap."""
    if not source.exists(): return
    owned = candidate.owned if candidate and candidate.safe else {}
    for path in source.rglob('*'):
        if not path.is_file(): continue
        relative = path.relative_to(source).as_posix()
        safe_child(source, relative)
        if relative in owned or relative == MARKER: continue
        destination = safe_child(stage, relative)
        if destination.exists(): continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def update_old_links(discovery, latest, cleaned_entries):
    import pythoncom
    import win32com.client
    initialized = False
    link = shell = None
    results = []
    try:
        try:
            pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
            initialized = True
        except pythoncom.com_error as exc:
            if exc.hresult != -2147417850: raise
        shell = win32com.client.Dispatch('WScript.Shell')
        for old in discovery.links:
            path = Path(old['path'])
            if not path.is_file(): continue
            link = shell.CreateShortcut(str(path))
            if link.TargetPath != old['target'] or link.Arguments != old['args']: continue
            matched = next((c for c in discovery.candidates if c.entry in cleaned_entries and
                           (str(c.entry).casefold() == old['target'].casefold() or
                            (c.kind == 'music' and str(c.root / 'launch.ps1').casefold() in old['args'].casefold()))), None)
            if matched is None: continue
            if matched.kind == 'kling' and re.match(r'^--batch(?:\s|=)', old['args']):
                exe = latest / 'tools/kling-batch/可灵批处理.exe'
                link.TargetPath = str(exe)
                link.WorkingDirectory = str(exe.parent)
                link.IconLocation = str(exe) + ',0'
                link.Save()
                results.append('已迁移任务入口')
            else:
                path.unlink()
                results.append('已移除旧快捷方式')
            link = None
        shell = None
    finally:
        link = shell = None
        if initialized: pythoncom.CoUninitialize()
    return results


def remove_old_registration(candidate, latest):
    import winreg
    key_name = VIDEO_REGKEY if candidate.kind == 'video' else (REGKEY if candidate.kind == 'suite' else None)
    if not key_name or candidate.entry.exists(): return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name) as key:
            registered = Path(winreg.QueryValueEx(key, 'InstallLocation')[0]).resolve()
        if registered == candidate.root.resolve() and registered != latest.resolve():
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_name)
    except OSError: pass
