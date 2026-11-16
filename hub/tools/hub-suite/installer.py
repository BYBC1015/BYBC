"""Per-user Windows installer; touches only its chosen application directory."""
from __future__ import annotations

import argparse
import ctypes
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import uuid
import winreg
import zipfile

from codex_bridge import __version__
from codex_bridge.startup import StartupManager
from codex_bridge import suite_upgrade as upgrade
from dataclasses import replace

VERSION = __version__
NAME = "Codex Bridge"
LEGACY_NAME = "Codex Hub 工具箱"
REGKEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\CodexHubSuite"
MARKER = ".hub-suite-install.json"
# This runtime document contains a user's Resolve installation path. Package
# defaults are verified as shipped, but an existing local document is user data.
LOCAL_PREFERENCES = ("tools/beat-workshop/runtime.json",)


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + '.writing-' + uuid.uuid4().hex)
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def operation_receipt(target: Path, status: str, message: str) -> None:
    """A native exit code alone cannot prove an installation reached its commit."""
    value = os.environ.get('CODEX_HUB_INSTALLER_RECEIPT_PATH')
    if not value:
        return  # Source-only callers do not have an outer launcher.
    nonce = os.environ.get('CODEX_HUB_INSTALLER_NONCE', '')
    operation = os.environ.get('CODEX_HUB_INSTALLER_OPERATION', '')
    receipt = Path(value)
    if not nonce or not operation or not receipt.is_absolute():
        raise ValueError('安装完成回执契约无效')
    atomic_json(receipt, {'schema': 1, 'nonce': nonce, 'version': VERSION,
        'operation': operation, 'target': str(target.resolve()),
        'status': status, 'message': message[:4096]})


def persist_install_state(root: Path, state: dict, *, receipt: bool = True) -> None:
    atomic_json(root / MARKER, state)
    report_path = upgrade.default_config_path().parent / 'upgrade-report.json'
    report_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(report_path, {key: state[key] for key in (
        'version', 'configuration', 'cleanup', 'cleanup_status', 'completion',
        'installation_committed', 'operation_nonce', 'status')})
    if receipt:
        operation_receipt(root, state['status'], state['completion'])


def inside(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if Path(relative).is_absolute() or not candidate.is_relative_to(root.resolve()) or candidate == root.resolve():
        raise ValueError("安装清单包含无效路径")
    return candidate


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def local_preference_path(root: Path, relative: str) -> Path:
    parts = Path(relative).parts
    if Path(relative).is_absolute() or any(part in {"..", "."} for part in parts):
        raise ValueError("本机偏好路径无效")
    candidate = root
    # Inspect each lexical segment before resolving/reading it. A directory
    # junction or a link to any location is not a transferable preference.
    for part in parts:
        candidate = candidate / part
        try:
            attributes = getattr(candidate.lstat(), "st_file_attributes", 0)
        except FileNotFoundError:
            attributes = 0
        if candidate.is_symlink() or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("本机偏好不能位于符号链接或目录联接中")
    return inside(root, relative)


def read_local_preferences(root: Path) -> dict[str, bytes]:
    saved = {}
    for relative in LOCAL_PREFERENCES:
        path = local_preference_path(root, relative)
        if not path.exists():
            continue
        if not path.is_file():
            raise ValueError("本机偏好必须是普通文件")
        saved[relative] = path.read_bytes()
    return saved


def preserve_local_preferences(stage: Path, saved: dict[str, bytes]) -> dict[str, str]:
    hashes = {}
    for relative, body in saved.items():
        if relative not in LOCAL_PREFERENCES:
            raise ValueError("未声明的本机偏好不能迁移")
        target = local_preference_path(stage, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        checksum = hashlib.sha256(body).hexdigest()
        if digest(target) != checksum:
            raise ValueError("本机偏好保留校验失败")
        hashes[relative] = checksum
    return hashes


def check_idle(root):
    # Check only this installation. Old standalone Bridge and user work stay online.
    import psutil
    for process in psutil.process_iter(["exe", "pid", "cmdline"]):
        try:
            if process.info["pid"] == os.getpid():
                continue
            exe = process.info["exe"]
            if exe and Path(exe).resolve().is_relative_to(root):
                raise RuntimeError("请先关闭此安装目录中的工具和代理，再重试。")
            # The music window uses Resolve's shared Electron executable.
            if exe and Path(exe).name.lower() in {"electron.exe", "resolvepython.exe"}:
                for argument in (process.info.get("cmdline") or [])[1:]:
                    if not argument.startswith("-") and Path(argument).is_absolute() and Path(argument).resolve().is_relative_to(root):
                        raise RuntimeError("请先关闭此安装目录中的节拍工坊，再重试。")
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue


@contextmanager
def com_context():
    import pythoncom
    initialized = False
    try:
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        initialized = True
    except pythoncom.com_error as exc:
        if exc.hresult != -2147417850:  # RPC_E_CHANGED_MODE: caller already initialized COM.
            raise
    try:
        yield
    finally:
        if initialized:
            pythoncom.CoUninitialize()


def shortcut(path, exe):
    import win32com.client
    link = shell = None
    with com_context():
        try:
            shell = win32com.client.Dispatch("WScript.Shell")
            link = shell.CreateShortcut(str(path))
            link.TargetPath = str(exe)
            link.Arguments = "--start"
            link.WorkingDirectory = str(exe.parent)
            link.Save()
        finally:
            link = shell = None


def shortcut_target(path):
    import win32com.client
    link = shell = None
    with com_context():
        try:
            shell = win32com.client.Dispatch("WScript.Shell")
            link = shell.CreateShortcut(str(path))
            return Path(link.TargetPath).resolve()
        finally:
            link = shell = None


def system_tar() -> Path:
    """Resolve the OS-owned extractor without consulting PATH or environment."""
    directory = ctypes.create_unicode_buffer(32768)
    count = ctypes.windll.kernel32.GetSystemDirectoryW(directory, len(directory))
    if not count or count >= len(directory):
        raise RuntimeError("无法读取 Windows 系统目录。")
    tool = Path(directory.value) / 'tar.exe'
    if not tool.is_file():
        raise RuntimeError("此安装包需要 Windows 10 1803 或更新版本的系统解压组件，请先更新 Windows。")
    return tool


@contextmanager
def payload_read_lock(path):
    # Keep the exact reviewed ZIP immutable until the system extractor exits.
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


def extract_verified_payload(archive_path, stage):
    """Validate before extraction and after it, before touching an old install."""
    if stage.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT or any(stage.iterdir()):
        raise ValueError('安装暂存目录必须是全新的普通空目录')
    with payload_read_lock(archive_path), zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if len(members) > 20_000 or sum(item.file_size for item in members) > 16 * 1024 ** 3:
            raise ValueError('安装包大小或文件数超出限制')
        if archive.getinfo('bundle-manifest.json').file_size > 8 * 1024 ** 2:
            raise ValueError('安装包清单过大')
        manifest = json.loads(archive.read('bundle-manifest.json'))
        expected = {item['path']: item for item in manifest['files']}
        names = [item.filename for item in members]
        if (len(expected) != len(manifest['files']) or len(names) != len({name.casefold() for name in names})
                or set(names) != set(expected) | {'bundle-manifest.json'}):
            raise ValueError('安装包清单校验失败')
        directories = set()
        for item in members:
            name = item.filename
            if ('\\' in name or ':' in name or name.startswith('/')
                    or any(part in ('', '.', '..') for part in name.split('/'))):
                raise ValueError('安装包包含无效路径')
            reserved = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} | {
                prefix + number for prefix in ('COM', 'LPT') for number in '123456789¹²³'}
            if any(part.endswith((' ', '.')) or any(ord(ch) < 32 or ch in '<>"|?*' for ch in part)
                   or part.split('.')[0].upper() in reserved for part in name.split('/')):
                raise ValueError('安装包包含 Windows 保留路径')
            # Reject path-bearing/nonstandard ZIP metadata before asking tar to
            # interpret it. Our release writer emits only optional ZIP64 sizes.
            extra = item.extra
            while extra:
                if len(extra) < 4:
                    raise ValueError('安装包扩展信息无效')
                kind, length = int.from_bytes(extra[:2], 'little'), int.from_bytes(extra[2:4], 'little')
                if kind not in (0x0001, 0x5455) or len(extra) < length + 4:
                    raise ValueError('安装包包含不支持的路径扩展信息')
                extra = extra[length + 4:]
            inside(stage, name)
            if (item.is_dir() or ((item.external_attr >> 16) & 0o170000) not in (0, stat.S_IFREG)
                    or item.external_attr & 0x400):
                raise ValueError('安装包包含链接或特殊文件')
            for parent in Path(name).parents:
                if str(parent) != '.': directories.add(parent.as_posix())
            if name in expected:
                record = expected[name]
                if ('bytes' in record and (type(record['bytes']) is not int or record['bytes'] != item.file_size)):
                    raise ValueError('安装包文件长度不符：' + name)
                with archive.open(item) as content:
                    if hashlib.file_digest(content, 'sha256').hexdigest() != record['sha256']:
                        raise ValueError('安装包校验失败：' + name)
        if {name.casefold() for name in names} & {name.casefold() for name in directories}:
            raise ValueError('安装包路径发生文件与目录冲突')
        result = subprocess.run([str(system_tar()), '-xf', str(archive_path), '-C', str(stage)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            failure = RuntimeError('Windows 标准解压未完成；现有安装未改动。')
            failure.add_note('System extractor diagnostics: ' + result.stderr[-16_000:].decode('utf-8', errors='replace'))
            raise failure
        if stage.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError('解压暂存目录成为链接')
        actual = set()
        for directory, folders, files in os.walk(stage, followlinks=False):
            parent = Path(directory)
            for name in folders + files:
                path = parent / name
                if path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise ValueError('解压结果包含链接')
            if parent != stage and parent.relative_to(stage).as_posix() not in directories:
                raise ValueError('解压结果包含未登记目录')
            actual.update((parent / name).relative_to(stage).as_posix() for name in files)
        if actual != set(names):
            raise ValueError('解压结果与安装包清单不一致')
        for name, record in expected.items():
            if digest(stage / name) != record['sha256']:
                raise ValueError('解压后的文件校验失败：' + name)
        if (stage / 'bundle-manifest.json').read_bytes() != archive.read('bundle-manifest.json'):
            raise ValueError('解压后的清单发生变化')
        return manifest, expected


def install(root, report=lambda text: None, shortcuts=True, registration=True, startup=True,
            inherit=True, cleanup_old=True, legacy_dirs=(), inherit_config=None, scan_system=True):
    if sys.getwindowsversion().major < 10:
        raise RuntimeError("此安装包需要 Windows 10 或 Windows 11。")
    root = root.expanduser().resolve()
    if root == Path(root.anchor) or root in {Path.home().resolve(), Path(os.environ["LOCALAPPDATA"]).resolve()}:
        raise ValueError("请选择独立的 CodexHub 安装文件夹")
    if root.exists() and any(root.iterdir()) and not (root / MARKER).is_file():
        raise ValueError("所选文件夹已有其他文件，请选择新的空文件夹。")
    check_idle(root)
    local_preferences = read_local_preferences(root)
    report("正在检测旧软件和已有配置…")
    detected = upgrade.discover(root, legacy_dirs, system=scan_system)
    for candidate in detected.candidates:
        if candidate.safe and candidate.kind in {'bridge','suite'}:
            check_idle(candidate.entry)
    migration = upgrade.config_plan(detected.config_paths, inherit=inherit, selected=inherit_config)
    report(f"检测到 {len(detected.candidates)} 个已有程序；{migration.status}。")
    root.parent.mkdir(parents=True, exist_ok=True)
    stage = root.parent / (root.name + ".staging-" + uuid.uuid4().hex)
    stage.mkdir()
    archive_path = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "payload.zip"
    report("正在安装内置工具…")
    manifest, expected = extract_verified_payload(archive_path, stage)
    original_candidate = next((c for c in detected.candidates if c.root.resolve() == root), None)
    upgrade.carry_user_files(root, stage, original_candidate)
    # The payload and pristine manifest have already been verified above. Keep
    # exact local bytes separately; never rewrite the public release manifest.
    preserved = preserve_local_preferences(stage, local_preferences)
    state = {"version": VERSION, "root": str(root),
             "files": [name for name in expected if name not in LOCAL_PREFERENCES] + ["bundle-manifest.json"],
             "local_preferences": preserved, "shortcuts": [], "registration": registration}
    (stage / MARKER).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    previous = None
    if root.exists():
        # Preserve the entire previous installation for rollback, including any
        # user-added files. Never remove an existing directory recursively.
        previous = root.with_name(root.name + ".previous-" + uuid.uuid4().hex[:8])
        root.rename(previous)
    try:
        stage.rename(root)
    except Exception:
        if previous:
            previous.rename(root)
        raise
    # No old program is deleted until all new files and the inherited config
    # have been verified. A late failure leaves the complete previous install.
    try:
        state["configuration"] = upgrade.apply_config(migration)
    except Exception:
        failed = root.with_name(root.name + '.failed-' + uuid.uuid4().hex[:8])
        root.rename(failed)
        if previous: previous.rename(root)
        raise
    if shortcuts:
        # Resolve redirected Desktop/Start Menu using the Windows shell.
        import win32com.client
        shell = None
        with com_context():
            try:
                shell = win32com.client.Dispatch("WScript.Shell")
                folders = [Path(shell.SpecialFolders("Desktop")), Path(shell.SpecialFolders("Programs"))]
            finally:
                shell = None
        for folder in folders:
            path = folder / (NAME + ".lnk")
            shortcut(path, root / "CodexBridge.exe")
            state["shortcuts"].append(str(path))
            legacy = folder / (LEGACY_NAME + ".lnk")
            if legacy.is_file() and shortcut_target(legacy) == root / "CodexBridge.exe":
                legacy.unlink()
    if startup:
        StartupManager().enable(root / "CodexBridge.exe", args=["--start"])
    state["startup"] = startup
    if registration:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGKEY) as key:
            values = {"DisplayName": NAME, "DisplayVersion": VERSION, "Publisher": "BYBC",
                      "InstallLocation": str(root), "DisplayIcon": str(root / "CodexBridge.exe"),
                      "UninstallString": subprocess.list2cmdline([str(root / "Uninstall.exe"), "--uninstall", str(root)]),
                      "QuietUninstallString": subprocess.list2cmdline([str(root / "Uninstall.exe"), "--uninstall", str(root), "--quiet"])}
            for name, value in values.items():
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    if previous:
        state["previous_installation"] = str(previous)
    (root / MARKER).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    for relative, item in expected.items():
        if digest(root / relative) != preserved.get(relative, item['sha256']):
            raise ValueError("安装后校验失败，旧版本已保留。")
    state['cleanup'] = []
    state['installation_committed'] = True
    state['operation_nonce'] = os.environ.get('CODEX_HUB_INSTALLER_NONCE', '')
    state['cleanup_status'] = 'pending' if cleanup_old and detected.candidates else ('complete' if cleanup_old else 'skipped')
    state['status'] = 'installed_cleanup_pending' if state['cleanup_status'] == 'pending' else 'installed'
    state['completion'] = f"安装完成。{state['configuration']['status']}。"
    if state['cleanup_status'] == 'pending':
        state['completion'] += ' 旧文件清理尚未完成，残余已保留；详见升级记录。'
    elif not cleanup_old:
        state['completion'] += ' 已按选择保留旧程序。'
    # Persist commit BEFORE optional cleanup. A native interruption during an
    # unlink must never erase proof that the new installation was verified, or
    # falsely claim the remaining old files were removed.
    persist_install_state(root, state)
    if cleanup_old:
        report("新版已验证，正在清理旧程序并保留个人数据…")
        cleaned_entries = set()
        for candidate in detected.candidates:
            original_entry = candidate.entry
            if candidate.root.resolve() == root:
                if previous is None: continue
                candidate = replace(candidate, root=previous, entry=previous / candidate.entry.name)
            try:
                outcome = upgrade.cleanup(candidate, protected_root=root, check_idle=check_idle)
            except (OSError, ValueError, RuntimeError) as error:
                outcome = {'path': str(candidate.root), 'can_clean': False,
                    'removed_files': 0, 'remaining_files': len(candidate.owned),
                    'status': '旧文件清理未完成，残余已保留：' + type(error).__name__}
            state['cleanup'].append(outcome)
            persist_install_state(root, state)
            if candidate.safe and not candidate.entry.exists():
                if original_entry.resolve() != (root / 'CodexBridge.exe').resolve():
                    cleaned_entries.add(original_entry)
                if scan_system: upgrade.remove_old_registration(candidate, root)
        if scan_system:
            try:
                state['shortcut_cleanup'] = upgrade.update_old_links(detected, root, cleaned_entries)
            except OSError:
                state['shortcut_cleanup'] = ['部分旧快捷方式被占用，已保留']
    removed = sum(item['removed_files'] for item in state['cleanup'])
    pending = sum(bool(not item['can_clean'] or item['remaining_files'] or '占用' in item['status']) for item in state['cleanup'])
    state['cleanup_status'] = ('pending' if pending else 'complete') if cleanup_old else 'skipped'
    state['status'] = 'installed_cleanup_pending' if pending else 'installed'
    state['completion'] = f"安装完成。{state['configuration']['status']}；清理 {removed} 个旧程序文件。"
    if pending: state['completion'] += f" {pending} 个占用、变化或未识别的旧程序已保留，详见升级记录。"
    persist_install_state(root, state)
    report(state['completion'])
    return state


def uninstall(root):
    root = root.resolve()
    state = json.loads((root / MARKER).read_text(encoding="utf-8"))
    if Path(state["root"]).resolve() != root:
        raise ValueError("安装目录与记录不一致")
    check_idle(root)
    targets = [inside(root, name) for name in state["files"]]
    state['uninstall_status'] = 'pending'
    state['uninstall_nonce'] = os.environ.get('CODEX_HUB_INSTALLER_NONCE', '')
    atomic_json(root / MARKER, state)
    operation_receipt(root, 'uninstall_pending', '卸载未完成，剩余程序文件已保留；配置与个人数据未删除。')
    StartupManager().remove_if_owned(root / "CodexBridge.exe")
    # Resolve and validate all paths before deleting any owned file.
    for path in targets:
        if path.is_file():
            path.unlink()
    for value in state.get("shortcuts", []):
        path = Path(value)
        if path.name not in {NAME + ".lnk", LEGACY_NAME + ".lnk"} or not path.is_file():
            continue
        if shortcut_target(path) == root / "CodexBridge.exe":
            path.unlink()
    if state.get("registration"):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGKEY) as key:
                location = winreg.QueryValueEx(key, "InstallLocation")[0]
            if Path(location).resolve() == root:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REGKEY)
        except FileNotFoundError:
            pass
    (root / MARKER).unlink()
    for folder in sorted({p.parent for p in targets} | {root}, key=lambda p: len(p.parts), reverse=True):
        while folder.is_relative_to(root):
            try:
                folder.rmdir()  # Only empty directories; user-added files survive.
            except (OSError, FileNotFoundError):
                break
            if folder == root:
                break
            folder = folder.parent
    operation_receipt(root, 'removed', '卸载完成；本机配置、个人文件和作品已保留。')


def main():
    if '--help' in sys.argv[1:] or '-h' in sys.argv[1:]:
        operation_receipt(Path(os.environ.get('CODEX_HUB_INSTALLER_TARGET') or '.'), 'help', '已显示安装器帮助。')
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-dir", type=Path, default=Path(os.environ["LOCALAPPDATA"]) / "Programs/CodexHub")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--no-shortcuts", action="store_true")
    parser.add_argument("--no-registration", action="store_true")
    parser.add_argument("--no-startup", action="store_true", help="Skip startup registration for isolated verification")
    parser.add_argument("--no-cleanup", action="store_true", help="Keep detected old programs")
    parser.add_argument("--no-inherit", action="store_true", help="Do not import an old configuration")
    parser.add_argument("--legacy-dir", action="append", type=Path, default=[], help="Additional old installation to inspect")
    parser.add_argument("--inherit-config", type=Path, help="Explicit old configuration when multiple profiles exist")
    parser.add_argument("--no-system-scan", action="store_true", help="Inspect only the target and explicit directories")
    parser.add_argument("--scan-only", type=Path, help="Save a credential-free detection report without installing")
    parser.add_argument("--uninstall", type=Path)
    parser.add_argument("--remove", type=Path)
    args = parser.parse_args()
    if args.scan_only:
        found = upgrade.discover(args.install_dir, args.legacy_dir, system=not args.no_system_scan)
        try:
            configuration = upgrade.config_plan(found.config_paths, inherit=not args.no_inherit, selected=args.inherit_config).summary()
        except ValueError as exc:
            configuration = {'status':str(exc)}
        args.scan_only.write_text(json.dumps({'programs':[c.summary() for c in found.candidates],'configuration':configuration}, ensure_ascii=False, indent=2), encoding='utf-8')
        operation_receipt(args.scan_only, 'scanned', '检测报告已保存。')
        return 0
    if args.uninstall:
        if not args.quiet and not messagebox.askyesno(NAME, "卸载 Bridge 和附属工具？本机配置、历史任务和成片将保留。"):
            operation_receipt(args.uninstall, 'cancelled', '已取消卸载。')
            return 0
        helper_dir = Path(tempfile.mkdtemp(prefix="codex-hub-uninstall-"))
        helper = helper_dir / "Uninstall.exe"
        shutil.copy2(sys.executable, helper)
        env = os.environ.copy()
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        command = [str(helper), "--remove", str(args.uninstall.resolve())]
        if args.quiet:
            command.append("--quiet")
        subprocess.Popen(command, env=env, cwd=helper_dir)
        operation_receipt(args.uninstall, 'launched', '卸载进程已启动，尚未确认清理完成。')
        return 0
    if args.remove:
        # Let the original one-file uninstaller finish before checking locks.
        for attempt in range(20):
            try:
                uninstall(args.remove)
                return 0
            except (PermissionError, RuntimeError):
                time.sleep(0.5)
        raise RuntimeError("文件仍在使用，请关闭工具后重试卸载。")
    if args.quiet:
        install(args.install_dir, shortcuts=not args.no_shortcuts, registration=not args.no_registration, startup=not args.no_startup,
                inherit=not args.no_inherit, cleanup_old=not args.no_cleanup, legacy_dirs=args.legacy_dir,
                inherit_config=args.inherit_config, scan_system=not args.no_system_scan)
        return 0
    window = tk.Tk()
    window.title(f"安装 {NAME} {VERSION}")
    window.geometry("760x620")
    window.resizable(False, False)
    panel = ttk.Frame(window, padding=22)
    panel.pack(fill="both", expand=True)
    ttk.Label(panel, text=NAME, font=("Segoe UI", 18, "bold")).pack(anchor="w")
    ttk.Label(panel, text="Bridge 开机自动连接。视频延长、可灵批处理、音乐制作从工具箱打开。节拍工坊需已安装达芬奇 Studio 21.1。", wraplength=710).pack(anchor="w", pady=(10, 12))
    location = tk.StringVar(value=str(args.install_dir))
    entry = ttk.Entry(panel, textvariable=location)
    entry.pack(fill="x")
    browse = ttk.Button(panel, text="选择安装位置", command=lambda: location.set(filedialog.askdirectory(initialdir=str(args.install_dir.parent)) or location.get()))
    browse.pack(anchor="w", pady=8)
    inherit_var = tk.BooleanVar(value=not args.no_inherit)
    cleanup_var = tk.BooleanVar(value=not args.no_cleanup)
    ttk.Checkbutton(panel, text="继承已有 Token 和配置（已有有效配置优先保留）", variable=inherit_var).pack(anchor='w')
    ttk.Checkbutton(panel, text="新版验证成功后清理旧程序（保留方案、历史和作品）", variable=cleanup_var).pack(anchor='w', pady=(4,8))
    scan_actions = ttk.Frame(panel)
    scan_actions.pack(fill='x')
    detected_list = ttk.Treeview(panel, columns=('name','version','path'), show='headings', height=5)
    for key, title, width in [('name','检测到的软件',150),('version','版本',95),('path','位置 / 状态',435)]:
        detected_list.heading(key,text=title)
        detected_list.column(key,width=width,stretch=key=='path')
    detected_list.pack(fill='x',pady=8)
    status = tk.StringVar(value="安装包不包含账号凭据。已有配置、历史和成片会保留。")
    ttk.Label(panel, textvariable=status, wraplength=710).pack(anchor="w", pady=8)
    progress = ttk.Progressbar(panel, mode="indeterminate")
    progress.pack(fill="x", pady=8)
    events = queue.Queue()
    extra_dirs = list(args.legacy_dir)
    selected_config = args.inherit_config
    def scan_now():
        destination = Path(location.get())
        scan_button.configure(state='disabled')
        button.configure(state='disabled')
        status.set('正在检测旧软件和已有配置…')
        selected = selected_config
        inherit_enabled = inherit_var.get()
        directories = list(extra_dirs)
        def work():
            try:
                found = upgrade.discover(destination, directories, system=not args.no_system_scan)
                config = upgrade.config_plan(found.config_paths, inherit=inherit_enabled, selected=selected)
                events.put(('scan', ([c.summary() for c in found.candidates], config.status)))
            except Exception as exc:
                events.put(('scan_error', str(exc)))
        threading.Thread(target=work,daemon=True).start()
    def choose_config():
        nonlocal selected_config
        value = filedialog.askopenfilename(title='选择旧版 Bridge 配置', filetypes=[('JSON 配置','*.json')])
        if value:
            selected_config = Path(value)
            scan_now()
    def choose_legacy():
        value = filedialog.askdirectory(title='选择旧程序所在文件夹')
        if value:
            extra_dirs.append(Path(value))
            scan_now()
    scan_button = ttk.Button(scan_actions,text='检测旧软件',command=scan_now)
    scan_button.pack(side='left')
    ttk.Button(scan_actions,text='添加旧程序位置',command=choose_legacy).pack(side='left',padx=8)
    ttk.Button(scan_actions,text='选择旧配置',command=choose_config).pack(side='left')
    def begin():
        destination = Path(location.get())
        inherit_enabled, cleanup_enabled = inherit_var.get(), cleanup_var.get()
        selected = selected_config
        button.configure(state="disabled")
        entry.configure(state="disabled")
        browse.configure(state="disabled")
        scan_button.configure(state='disabled')
        window.protocol("WM_DELETE_WINDOW", lambda: None)
        progress.start()
        def work():
            try:
                state = install(destination, lambda text: events.put(("status", text)),
                                shortcuts=not args.no_shortcuts, registration=not args.no_registration,
                                startup=not args.no_startup, inherit=inherit_enabled,
                                cleanup_old=cleanup_enabled, legacy_dirs=list(extra_dirs), inherit_config=selected,
                                scan_system=not args.no_system_scan)
                events.put(("done", (str(destination), state['completion'])))
            except Exception as exc:
                events.put(("error", str(exc)))
        threading.Thread(target=work, daemon=True).start()
    def poll():
        try:
            kind, value = events.get_nowait()
            if kind == "status":
                status.set(value)
            elif kind in {'scan','scan_error'}:
                scan_button.configure(state='normal')
                button.configure(state='normal')
                for row in detected_list.get_children(): detected_list.delete(row)
                if kind == 'scan_error': status.set(value)
                else:
                    items, config_status = value
                    for item in items:
                        detected_list.insert('', 'end', values=(item['name'],item['version'],item['path'] if item['can_clean'] else item['reason']))
                    status.set(f"检测到 {len(items)} 个已有程序。{config_status}。")
            else:
                progress.stop()
                window.protocol("WM_DELETE_WINDOW", window.destroy)
                if kind == "done":
                    destination, message = value
                    status.set(message)
                    def launch():
                        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1')
                        subprocess.Popen([str(Path(destination) / 'CodexBridge.exe'),'--start'], env=env, shell=False)
                        window.destroy()
                    button.configure(text="启动 Bridge", state="normal", command=launch)
                else:
                    messagebox.showerror(NAME, value, parent=window)
                    button.configure(state="normal")
                    entry.configure(state="normal")
                    browse.configure(state="normal")
                    scan_button.configure(state='normal')
        except queue.Empty:
            pass
        window.after(100, poll)
    button = ttk.Button(panel, text="安装", command=begin)
    button.pack(anchor="e")
    poll()
    scan_now()
    window.mainloop()
    receipt_path = os.environ.get('CODEX_HUB_INSTALLER_RECEIPT_PATH')
    if not receipt_path or not Path(receipt_path).is_file():
        operation_receipt(Path(location.get()), 'cancelled', '已关闭安装器，未完成安装。')
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log = Path(tempfile.gettempdir()) / "CodexHubInstaller.log"
        with log.open("a", encoding="utf-8") as stream:
            stream.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + traceback.format_exc() + "\n")
        if "--quiet" not in sys.argv:
            messagebox.showerror(NAME, str(exc))
        raise SystemExit(1)
