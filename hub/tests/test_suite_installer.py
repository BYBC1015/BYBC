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


def preference_payload(tmp_path, monkeypatch):
    """Both release manifests own a default; the user edits their old copy."""
    root = old_and_new_payload(tmp_path, monkeypatch)
    relative = 'tools/beat-workshop/runtime.json'
    preference = root / relative
    preference.parent.mkdir(parents=True)
    preference.write_bytes(b'{}')
    manifest_path = root/'bundle-manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['files'].append({'path':relative,'sha256':installer.digest(preference)})
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    catalog = {'entries':{installer.digest(root/'CodexBridge.exe'):{'kind':'bridge','version':'1.0.25'}},
               'manifests':[installer.digest(manifest_path)],'files':{'kling':{},'music':{},'video':{}}}
    monkeypatch.setattr(installer.upgrade,'load_catalog',lambda:catalog)
    archive_path = tmp_path/'source/payload.zip'
    with zipfile.ZipFile(archive_path) as archive:
        new_manifest = json.loads(archive.read('bundle-manifest.json'))
        new_exe = archive.read('CodexBridge.exe')
    new_manifest['files'].append({'path':relative,'sha256':installer.hashlib.sha256(b'{}').hexdigest()})
    with zipfile.ZipFile(archive_path,'w') as archive:
        archive.writestr('bundle-manifest.json',json.dumps(new_manifest))
        archive.writestr('CodexBridge.exe',new_exe)
        archive.writestr(relative,b'{}')
    return root, relative, new_manifest


def test_upgrade_keeps_exact_local_preferences_and_uninstall_keeps_user_configuration(tmp_path,monkeypatch):
    root,relative,new_manifest = preference_payload(tmp_path,monkeypatch)
    existing = '\ufeff{\r\n  "resolveRoot": "D:/用户工具/Resolve", "extra": {"keep": true}\r\n}\r\n'.encode('utf-8')
    (root/relative).write_bytes(existing)
    state=installer.install(root,shortcuts=False,registration=False,startup=False,scan_system=False)
    assert (root/'CodexBridge.exe').read_bytes()==b'new bridge'
    assert (root/relative).read_bytes()==existing
    assert relative not in state['files']
    assert state['local_preferences']=={relative:installer.hashlib.sha256(existing).hexdigest()}
    assert json.loads((root/'bundle-manifest.json').read_text())==new_manifest
    state=installer.install(root,shortcuts=False,registration=False,startup=False,scan_system=False)
    assert (root/relative).read_bytes()==existing
    installer.uninstall(root)
    assert not (root/'CodexBridge.exe').exists()
    assert (root/relative).read_bytes()==existing


def test_absent_local_preference_is_not_invented_by_preservation(tmp_path,monkeypatch):
    root=old_and_new_payload(tmp_path,monkeypatch)
    state=installer.install(root,shortcuts=False,registration=False,startup=False,scan_system=False)
    assert state['local_preferences']=={}
    assert not (root/'tools/beat-workshop/runtime.json').exists()


def test_unmodified_shipped_preference_is_user_owned_after_fresh_install(tmp_path,monkeypatch):
    root,relative,_=preference_payload(tmp_path,monkeypatch)
    (root/relative).unlink()
    state=installer.install(root,shortcuts=False,registration=False,startup=False,scan_system=False)
    assert state['local_preferences']=={} and (root/relative).read_bytes()==b'{}'
    assert relative not in state['files']
    (root/relative).write_text('{"resolveRoot":"D:/Resolve"}')
    installer.uninstall(root)
    assert (root/relative).read_text()=='{"resolveRoot":"D:/Resolve"}'


@pytest.mark.parametrize('relative',['../outside.json','C:/outside.json'])
def test_local_preference_paths_cannot_escape(tmp_path,relative):
    with pytest.raises(ValueError):
        installer.local_preference_path(tmp_path/'install',relative)
    with pytest.raises(ValueError,match='未声明'):
        installer.preserve_local_preferences(tmp_path/'stage',{relative:b'private'})


@pytest.mark.parametrize('link_kind',['directory','file'])
def test_linked_local_preferences_rejected_before_install_without_reading_target(tmp_path,monkeypatch,link_kind):
    root=old_and_new_payload(tmp_path,monkeypatch)
    outside=tmp_path/'outside';outside.mkdir()
    private=outside/'runtime.json';private.write_bytes(b'private outside content')
    tools=root/'tools';tools.mkdir()
    link=tools/'beat-workshop' if link_kind=='directory' else tools/'beat-workshop/runtime.json'
    if link_kind=='file':link.parent.mkdir()
    try:
        link.symlink_to(outside if link_kind=='directory' else private,target_is_directory=link_kind=='directory')
    except OSError:
        if link_kind=='file':
            pytest.skip('Windows file symlink permission unavailable; directory junction is exercised separately')
        import subprocess
        quote=lambda path: "'"+str(path).replace("'","''")+"'"
        assert link.absolute().is_relative_to(tmp_path.absolute()) and outside.absolute().is_relative_to(tmp_path.absolute())
        subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',
            'New-Item -ItemType Junction -Path '+quote(link)+' -Target '+quote(outside)+' | Out-Null'],
            check=True,capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
    original_read=Path.read_bytes
    def guarded_read(path):
        assert not path.resolve().is_relative_to(outside.resolve()), 'Linked preference must not be read'
        return original_read(path)
    monkeypatch.setattr(Path,'read_bytes',guarded_read)
    with pytest.raises(ValueError,match='符号链接'):
        installer.install(root,shortcuts=False,registration=False,startup=False,scan_system=False)
    assert (root/'CodexBridge.exe').read_bytes()==b'old bridge'
    assert not list(root.parent.glob(root.name+'.previous-*'))
    if link_kind=='directory':
        link.rmdir()  # Remove only this test junction, never the external target.


def test_file_link_marker_is_rejected_before_any_preference_read(tmp_path,monkeypatch):
    root=old_and_new_payload(tmp_path,monkeypatch)
    preference=root/'tools/beat-workshop/runtime.json'
    preference.parent.mkdir(parents=True)
    preference.write_bytes(b'private')
    original_is_symlink=Path.is_symlink
    monkeypatch.setattr(Path,'is_symlink',lambda path:path==preference or original_is_symlink(path))
    monkeypatch.setattr(Path,'read_bytes',lambda path:pytest.fail('No linked preference content may be read'))
    with pytest.raises(ValueError,match='符号链接'):
        installer.read_local_preferences(root)
