import importlib.util
import json
from pathlib import Path
import sys
import zipfile
import uuid
import winreg

import pytest

SPEC = importlib.util.spec_from_file_location("suite_installer", Path(__file__).resolve().parents[1] / "tools/hub-suite/installer.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def payload(tmp_path, monkeypatch, corrupt=False):
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    discover = installer.upgrade.discover
    monkeypatch.setattr(installer.upgrade, 'discover', lambda root, dirs=(), **kw: discover(root, dirs, system=False))
    source = tmp_path / "source"
    source.mkdir()
    test_file = source / "app.exe"
    test_file.write_bytes(b"runtime")
    manifest = {"files": [{"path": "app.exe", "sha256": installer.digest(test_file)}]}
    with zipfile.ZipFile(source / "payload.zip", "w") as archive:
        archive.writestr("bundle-manifest.json", json.dumps(manifest))
        archive.writestr("app.exe", b"broken" if corrupt else b"runtime")
    monkeypatch.setattr(sys, "_MEIPASS", str(source), raising=False)
    monkeypatch.setattr(installer, "check_idle", lambda root: None)


def test_install_verify_upgrade_rollback_copy_and_uninstall_preserves_user_files(tmp_path, monkeypatch):
    payload(tmp_path, monkeypatch)
    root = tmp_path / "安装 folder"
    state = installer.install(root, shortcuts=False, registration=False)
    assert (root / "app.exe").read_bytes() == b"runtime"
    (root / "user-file.txt").write_text("keep")
    state = installer.install(root, shortcuts=False, registration=False)
    assert (Path(state["previous_installation"]) / "user-file.txt").read_text() == "keep"
    (root / "new-user-file.txt").write_text("also keep")
    installer.uninstall(root)
    assert not (root / "app.exe").exists()
    assert (root / "new-user-file.txt").read_text() == "also keep"


def test_corrupt_payload_cannot_replace_install(tmp_path, monkeypatch):
    payload(tmp_path, monkeypatch, corrupt=True)
    root = tmp_path / "install"
    with pytest.raises(ValueError, match="校验失败"):
        installer.install(root, shortcuts=False, registration=False)
    assert not root.exists()


def test_non_app_directory_rejected(tmp_path, monkeypatch):
    root = tmp_path / "personal"
    root.mkdir()
    (root / "important.txt").write_text("keep")
    with pytest.raises(ValueError, match="其他文件"):
        installer.install(root, shortcuts=False, registration=False)
    assert (root / "important.txt").read_text() == "keep"


@pytest.mark.parametrize("name", ["../outside.txt", "C:/outside.txt"])
def test_manifest_paths_cannot_escape_installation(tmp_path, name):
    with pytest.raises(ValueError):
        installer.inside(tmp_path / "install", name)


def test_uninstall_validates_all_paths_before_removing_owned_files(tmp_path, monkeypatch):
    root = tmp_path / "install"
    root.mkdir()
    (root / "safe.txt").write_text("keep")
    (root / installer.MARKER).write_text(json.dumps({"root": str(root), "files": ["safe.txt", "../outside.txt"]}))
    monkeypatch.setattr(installer, "check_idle", lambda root: None)
    with pytest.raises(ValueError):
        installer.uninstall(root)
    assert (root / "safe.txt").read_text() == "keep"


def test_windows_shortcut_and_registry_round_trip(tmp_path, monkeypatch):
    import win32com.client
    payload(tmp_path, monkeypatch)
    key_name = installer.REGKEY + "-Test-" + uuid.uuid4().hex
    monkeypatch.setattr(installer, "REGKEY", key_name)
    root = tmp_path / "installed"
    try:
        installer.install(root, shortcuts=False, registration=True)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name) as key:
            assert winreg.QueryValueEx(key, "InstallLocation")[0] == str(root)
        link_path = tmp_path / "toolbox.lnk"
        installer.shortcut(link_path, root / "CodexBridge.exe")
        with installer.com_context():
            link = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(link_path))
            assert Path(link.TargetPath) == root / "CodexBridge.exe"
            assert link.Arguments == "--start"
            link = None
        installer.uninstall(root)
        with pytest.raises(FileNotFoundError):
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name)
    finally:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_name)
        except FileNotFoundError:
            pass


def test_install_enables_bridge_startup_and_uninstall_removes_it(tmp_path, monkeypatch):
    payload(tmp_path, monkeypatch)
    root = tmp_path / "中文 安装"
    state = installer.install(root, shortcuts=False, registration=False)
    startup = installer.StartupManager()
    command = startup.command_path.read_text(encoding="utf-16")
    assert state["startup"] is True
    assert str(root / "CodexBridge.exe") in command
    assert "--start" in command and "--tools" not in command
    installer.uninstall(root)
    assert not startup.is_enabled()


def test_uninstall_preserves_startup_of_another_installation(tmp_path, monkeypatch):
    payload(tmp_path, monkeypatch)
    root = tmp_path / "old"
    installer.install(root, shortcuts=False, registration=False, startup=False)
    startup = installer.StartupManager()
    assert not startup.is_enabled()
    startup.enable(tmp_path / "new" / "CodexBridge.exe", args=["--start"])
    before = startup.command_path.read_bytes()
    installer.uninstall(root)
    assert startup.command_path.read_bytes() == before


def test_busy_music_uses_shared_electron_but_blocks_only_its_installation(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import psutil
    root = tmp_path / "安装"
    info = {"exe": str(tmp_path / "Resolve/Electron/electron.exe"), "pid": 99999,
            "cmdline": ["electron.exe", str(root / "tools/beat-workshop")]}
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: [SimpleNamespace(info=info)])
    with pytest.raises(RuntimeError, match="节拍工坊"):
        installer.check_idle(root)
    installer.check_idle(tmp_path / "other")


def old_and_new_payload(tmp_path, monkeypatch):
    payload(tmp_path, monkeypatch)
    root = tmp_path / '旧 安装'
    root.mkdir()
    exe = root / 'CodexBridge.exe'
    exe.write_bytes(b'old bridge')
    manifest = {'version':'1.0.25','files':[{'path':exe.name,'sha256':installer.digest(exe)}]}
    (root / 'bundle-manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    (root / installer.MARKER).write_text(json.dumps({'version':'1.0.25','root':str(root),'files':[exe.name,'bundle-manifest.json']}),encoding='utf-8')
    catalog = {'entries':{installer.digest(exe):{'kind':'bridge','version':'1.0.25'}},
               'manifests':[installer.digest(root/'bundle-manifest.json')], 'files':{'kling':{},'music':{},'video':{}}}
    monkeypatch.setattr(installer.upgrade,'load_catalog',lambda:catalog)
    new_exe = tmp_path / 'source/new.exe'
    new_exe.write_bytes(b'new bridge')
    new_manifest = {'version':installer.VERSION,'files':[{'path':'CodexBridge.exe','sha256':installer.digest(new_exe)}]}
    with zipfile.ZipFile(tmp_path/'source/payload.zip','w') as archive:
        archive.writestr('bundle-manifest.json',json.dumps(new_manifest))
        archive.writestr('CodexBridge.exe',new_exe.read_bytes())
    return root


def test_upgrade_inherits_tokens_then_cleans_old_binary_and_preserves_user_data(tmp_path, monkeypatch):
    root = old_and_new_payload(tmp_path,monkeypatch)
    (root/'config.json').write_text(json.dumps({'hub':'https://fixture.test/hub','token':'fixture-secret'}),encoding='utf-8')
    (root/'work.txt').write_text('my work')
    state = installer.install(root,shortcuts=False,registration=False,startup=False,scan_system=False)
    assert (root/'CodexBridge.exe').read_bytes() == b'new bridge'
    assert (root/'work.txt').read_text() == 'my work'
    assert installer.upgrade.read_config(installer.upgrade.default_config_path())['token'] == 'fixture-secret'
    assert not (Path(state['previous_installation'])/'CodexBridge.exe').exists()
    assert state['cleanup'][0]['removed_files'] == 2
    assert 'fixture-secret' not in json.dumps(state)
    assert 'fixture-secret' not in (installer.upgrade.default_config_path().parent/'upgrade-report.json').read_text(encoding='utf-8')


def test_migration_failure_restores_original_installation_without_cleanup(tmp_path, monkeypatch):
    root = old_and_new_payload(tmp_path,monkeypatch)
    def fail(plan): raise OSError('migration failed')
    monkeypatch.setattr(installer.upgrade,'apply_config',fail)
    with pytest.raises(OSError,match='migration failed'):
        installer.install(root,shortcuts=False,registration=False,startup=False,scan_system=False)
    assert (root/'CodexBridge.exe').read_bytes() == b'old bridge'
    assert (root/'bundle-manifest.json').exists()


def test_cleanup_can_be_disabled(tmp_path,monkeypatch):
    root = old_and_new_payload(tmp_path,monkeypatch)
    state = installer.install(root,shortcuts=False,registration=False,startup=False,cleanup_old=False,scan_system=False)
    assert (Path(state['previous_installation'])/'CodexBridge.exe').read_bytes() == b'old bridge'
    assert state['cleanup'] == []
