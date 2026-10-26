import hashlib
import json
from pathlib import Path

import pytest

from codex_bridge import suite_upgrade as u


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('APPDATA', str(tmp_path / 'Roaming'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'Local'))
    monkeypatch.setenv('PROGRAMDATA', str(tmp_path / 'ProgramData'))
    monkeypatch.setattr(u, 'windows_locations', lambda: ([], []))
    return tmp_path


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


def fixture_release(root, *, version='1.0.25', kind='suite'):
    root.mkdir(parents=True)
    exe = root / ('CodexBridge.exe' if kind in {'suite','bridge'} else '可灵批处理.exe')
    exe.write_bytes(b'known old runtime')
    catalog = {'entries':{u.digest(exe):{'kind':'bridge' if kind in {'suite','bridge'} else kind, 'version':version}},
               'manifests':[], 'files':{'kling':{}, 'music':{}, 'video':{}}}
    if kind == 'suite':
        manifest = write_json(root / 'bundle-manifest.json', {'version':version,'files':[{'path':exe.name,'sha256':u.digest(exe)}]})
        catalog['manifests'].append(u.digest(manifest))
        write_json(root / u.MARKER, {'version':version, 'root':str(root),'files':[exe.name,'bundle-manifest.json']})
    elif kind == 'kling': catalog['files']['kling'][exe.name] = [u.digest(exe)]
    return exe, catalog


def test_discovery_deduplicates_and_finds_custom_portable_config(isolated):
    root = isolated / '旧版 中文'
    exe, catalog = fixture_release(root)
    config = write_json(root / 'config.json', {'hub':'https://example.test/hub','token':'fixture-secret'})
    result = u.discover(root, [root, exe], system=False, catalog=catalog)
    assert len(result.candidates) == 1
    assert result.candidates[0].kind == 'suite'
    assert config in result.config_paths
    assert 'fixture-secret' not in json.dumps(result.candidates[0].summary())


def test_current_configuration_reused_without_any_byte_change(isolated):
    current = write_json(u.default_config_path(), {'hub':'https://example.test/hub','token':'current-secret','bindings':[{'id':'hermes','token':'hermes-secret','execution_backend':'hermes'}]})
    old = write_json(isolated / 'old.json', {'hub':'https://other.test','token':'other-secret'})
    before = current.read_bytes()
    plan = u.config_plan([old])
    summary = u.apply_config(plan)
    assert current.read_bytes() == before
    assert summary['credentials'] == 2
    assert 'secret' not in json.dumps(summary)


def test_imports_all_bindings_and_paths_without_exposing_tokens(isolated):
    old = write_json(isolated / '旧 目录/config.json', {'hub':'https://example.test/hub','token':'fixture-secret',
        'projects':{'project':'projects/project'}, 'state_path':'state.json',
        'bindings':[{'id':'hermes','token':'hermes-fixture-secret','execution_backend':'hermes'}]})
    before = old.read_bytes()
    plan = u.config_plan([old])
    assert 'fixture-secret' not in repr(plan)
    result = u.apply_config(plan)
    imported = u.read_config(u.default_config_path())
    assert imported['token'] == 'fixture-secret'
    assert imported['bindings'][0]['token'] == 'hermes-fixture-secret'
    assert imported['state_path'] == str(old.parent / 'state.json')
    assert old.read_bytes() == before
    assert 'secret' not in json.dumps(result)


def test_placeholder_is_backed_up_before_migration(isolated):
    target = write_json(u.default_config_path(), {'hub':'https://example.test','token':'PASTE_DEVICE_TOKEN_HERE'})
    before = target.read_bytes()
    source = write_json(isolated / 'old.json', {'hub':'https://example.test','token':'fixture-secret'})
    u.apply_config(u.config_plan([source]))
    backups = list(target.parent.glob('config.before-upgrade-*.json'))
    assert len(backups) == 1 and backups[0].read_bytes() == before


def test_conflicting_sources_require_selection_and_never_write(isolated):
    a = write_json(isolated / 'a.json', {'hub':'https://example.test','token':'a-secret'})
    b = write_json(isolated / 'b.json', {'hub':'https://example.test','token':'b-secret'})
    with pytest.raises(ValueError, match='多个不同') as error: u.config_plan([a,b])
    assert 'secret' not in str(error.value)
    assert not u.default_config_path().exists()
    u.apply_config(u.config_plan([a,b], selected=b))
    assert u.read_config(u.default_config_path())['token'] == 'b-secret'


def test_corrupt_current_config_blocks_inheritance_and_is_preserved(isolated):
    target = u.default_config_path()
    target.parent.mkdir(parents=True)
    target.write_text('{"token":"fixture-secret",', encoding='utf-8')
    before = target.read_bytes()
    with pytest.raises(ValueError) as error: u.config_plan([])
    assert 'fixture-secret' not in str(error.value)
    assert target.read_bytes() == before


def test_concurrent_config_edit_is_never_overwritten(isolated):
    source = write_json(isolated / 'old.json', {'hub':'https://example.test','token':'old-secret'})
    plan = u.config_plan([source])
    target = write_json(u.default_config_path(), {'hub':'https://example.test','token':'new-secret'})
    with pytest.raises(RuntimeError, match='发生变化'): u.apply_config(plan)
    assert u.read_config(target)['token'] == 'new-secret'


def test_atomic_migration_failure_leaves_original_config(isolated, monkeypatch):
    target = write_json(u.default_config_path(), {'hub':'https://example.test','token':''})
    original = target.read_bytes()
    source = write_json(isolated / 'old.json', {'hub':'https://example.test','token':'old-secret'})
    plan = u.config_plan([source])
    def fail(*a): raise OSError('disk failure')
    monkeypatch.setattr(u.os, 'replace', fail)
    with pytest.raises(OSError): u.apply_config(plan)
    assert target.read_bytes() == original
    assert not list(target.parent.glob('*.tmp'))


def test_cleanup_removes_only_verified_program_and_preserves_config_and_work(isolated):
    old = isolated / 'old'
    exe, catalog = fixture_release(old)
    config = write_json(old / 'config.json', {'hub':'https://example.test','token':'keep-secret'})
    work = old / 'my-work.txt'
    work.write_text('keep')
    result = u.cleanup(u.identify(old,catalog), protected_root=isolated/'new', check_idle=lambda root:None)
    assert not exe.exists()
    assert result['removed_files'] == 2
    assert config.exists() and work.read_text() == 'keep'


def test_running_program_and_current_install_are_never_deleted(isolated):
    old = isolated / 'old'
    exe, catalog = fixture_release(old)
    candidate = u.identify(old,catalog)
    def busy(root): raise RuntimeError('running')
    assert u.cleanup(candidate, protected_root=isolated/'new', check_idle=busy)['removed_files'] == 0
    assert u.cleanup(candidate, protected_root=old, check_idle=lambda root:None)['removed_files'] == 0
    assert exe.exists()


def test_changed_executable_is_kept_even_after_detection(isolated):
    exe, catalog = fixture_release(isolated/'old')
    candidate = u.identify(exe.parent,catalog)
    exe.write_bytes(b'user changed file')
    result = u.cleanup(candidate, protected_root=isolated/'new', check_idle=lambda root:None)
    assert exe.read_bytes() == b'user changed file'
    assert result['remaining_files'] == 1


def test_forged_manifest_is_rejected(isolated):
    exe,catalog = fixture_release(isolated/'old')
    write_json(exe.parent/'bundle-manifest.json',{'version':'1.0.25','files':[{'path':'../personal.txt','sha256':'x'}]})
    candidate = u.identify(exe.parent,catalog)
    assert not candidate.safe
    assert u.cleanup(candidate, protected_root=isolated/'new', check_idle=lambda root:None)['removed_files'] == 0
    assert exe.exists()


def test_newer_bridge_is_preserved_but_kling_uses_its_own_version(isolated):
    exe,catalog = fixture_release(isolated/'newer',version='9.0.0',kind='bridge')
    assert not u.identify(exe,catalog).safe
    exe,catalog = fixture_release(isolated/'kling',version='1.1.1',kind='kling')
    assert u.identify(exe,catalog).safe


def test_user_files_stay_at_original_path_during_upgrade(isolated):
    old = isolated/'old'
    exe,catalog = fixture_release(old)
    write_json(old/'settings/config.json', {'personal':'keep'})
    stage = isolated/'stage'
    stage.mkdir()
    (stage/exe.name).write_bytes(b'new runtime')
    u.carry_user_files(old,stage,u.identify(old,catalog))
    assert (stage/'settings/config.json').read_bytes() == (old/'settings/config.json').read_bytes()
    assert (stage/exe.name).read_bytes() == b'new runtime'


def test_directory_link_cannot_escape_cleanup_root(isolated):
    import subprocess
    old = isolated/'old'
    old.mkdir()
    outside = isolated/'personal'
    outside.mkdir()
    (outside/'work.txt').write_text('keep')
    link = old / 'linked'
    try: link.symlink_to(outside, target_is_directory=True)
    except OSError:
        quote = lambda p: "'" + str(p).replace("'", "''") + "'"
        assert link.absolute().is_relative_to(isolated.absolute())
        assert outside.absolute().is_relative_to(isolated.absolute())
        subprocess.run(['powershell.exe','-NoProfile','-Command',
                        'New-Item -ItemType Junction -Path ' + quote(link) + ' -Target ' + quote(outside) + ' | Out-Null'],
                       check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    with pytest.raises(ValueError): u.safe_child(old,'linked/work.txt')
    assert (outside/'work.txt').read_text() == 'keep'
    link.rmdir()  # Remove only the junction, never the target directory.


def test_task_shortcuts_keep_batch_arguments_and_move_to_latest(isolated):
    import win32com.client
    import pythoncom
    pythoncom.CoInitialize()
    try:
        exe,catalog = fixture_release(isolated/'old',kind='kling',version='1.1.1')
        linkpath = exe.parent/'查看任务.lnk'
        shell = win32com.client.Dispatch('WScript.Shell')
        link = shell.CreateShortcut(str(linkpath))
        link.TargetPath = str(exe)
        link.Arguments = '--batch "C:\\我的作品\\batch.json"'
        link.Save()
        links = u.read_task_links(exe.parent)
        candidate = u.identify(exe,catalog)
        latest = isolated/'latest'
        target = latest/'tools/kling-batch/可灵批处理.exe'
        target.parent.mkdir(parents=True)
        target.write_bytes(b'new')
        exe.unlink()
        result = u.update_old_links(u.Discovery([candidate],[],links),latest,{exe})
        link = shell.CreateShortcut(str(linkpath))
        assert Path(link.TargetPath) == target
        assert link.Arguments == '--batch "C:\\我的作品\\batch.json"'
        assert result == ['已迁移任务入口']
    finally:
        link = shell = None
        pythoncom.CoUninitialize()
