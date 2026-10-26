"""Per-user Windows installer; touches only its chosen application directory."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
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


def inside(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if Path(relative).is_absolute() or not candidate.is_relative_to(root.resolve()) or candidate == root.resolve():
        raise ValueError("安装清单包含无效路径")
    return candidate


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


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
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("bundle-manifest.json"))
        expected = {item["path"]: item for item in manifest["files"]}
        if len(expected) != len(manifest["files"]) or len(archive.namelist()) != len(set(archive.namelist())) or set(archive.namelist()) != set(expected) | {"bundle-manifest.json"}:
            raise ValueError("安装包清单校验失败")
        for relative in archive.namelist():
            target = inside(stage, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(relative) as source, target.open("wb") as dest:
                shutil.copyfileobj(source, dest)
            if relative in expected and digest(target) != expected[relative]["sha256"]:
                raise ValueError("安装包校验失败：" + relative)
    original_candidate = next((c for c in detected.candidates if c.root.resolve() == root), None)
    upgrade.carry_user_files(root, stage, original_candidate)
    state = {"version": VERSION, "root": str(root), "files": list(expected) + ["bundle-manifest.json"], "shortcuts": [], "registration": registration}
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
        if digest(root / relative) != item['sha256']:
            raise ValueError("安装后校验失败，旧版本已保留。")
    state['cleanup'] = []
    if cleanup_old:
        report("新版已验证，正在清理旧程序并保留个人数据…")
        cleaned_entries = set()
        for candidate in detected.candidates:
            original_entry = candidate.entry
            if candidate.root.resolve() == root:
                if previous is None: continue
                candidate = replace(candidate, root=previous, entry=previous / candidate.entry.name)
            outcome = upgrade.cleanup(candidate, protected_root=root, check_idle=check_idle)
            state['cleanup'].append(outcome)
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
    state['completion'] = f"安装完成。{state['configuration']['status']}；清理 {removed} 个旧程序文件。"
    if pending: state['completion'] += f" {pending} 个占用、变化或未识别的旧程序已保留，详见升级记录。"
    (root / MARKER).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path = upgrade.default_config_path().parent / 'upgrade-report.json'
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({'version':VERSION,'configuration':state['configuration'],'cleanup':state['cleanup'],'completion':state['completion']}, ensure_ascii=False, indent=2), encoding='utf-8')
    report(state['completion'])
    return state


def uninstall(root):
    root = root.resolve()
    state = json.loads((root / MARKER).read_text(encoding="utf-8"))
    if Path(state["root"]).resolve() != root:
        raise ValueError("安装目录与记录不一致")
    check_idle(root)
    targets = [inside(root, name) for name in state["files"]]
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


def main():
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
        return 0
    if args.uninstall:
        if not args.quiet and not messagebox.askyesno(NAME, "卸载 Bridge 和附属工具？本机配置、历史任务和成片将保留。"):
            return 0
        helper_dir = Path(tempfile.mkdtemp(prefix="codex-hub-uninstall-"))
        helper = helper_dir / "Uninstall.exe"
        shutil.copy2(sys.executable, helper)
        env = os.environ.copy()
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        subprocess.Popen([str(helper), "--remove", str(args.uninstall.resolve())], env=env, cwd=helper_dir)
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
