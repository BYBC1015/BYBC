"""Release staging accepts audited public inputs, never arbitrary installed data."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import zipfile

import pytest

TOOL_DIR = Path(__file__).resolve().parents[1] / 'tools/hub-suite'
sys.path.insert(0, str(TOOL_DIR))
SPEC = importlib.util.spec_from_file_location('desktop_package_build', TOOL_DIR / 'build_desktop.py')
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def archive_at(tmp_path, entries):
    archive = tmp_path / 'release.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        for name, value in entries:
            output.writestr(name, value)
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest()


def test_verified_archive_extracts_only_selected_public_roots(tmp_path):
    archive, checksum = archive_at(tmp_path, [('Release/tool.exe', b'public'),
        ('Release/licenses/LICENSE.txt', b'license'), ('Release/config.json', b'private fixture')])
    destination = tmp_path / 'payload'
    builder.extract_public_archive(archive, checksum, destination, 'Release', {'tool.exe', 'licenses'})
    assert (destination / 'tool.exe').read_bytes() == b'public'
    assert (destination / 'licenses/LICENSE.txt').read_bytes() == b'license'
    assert not (destination / 'config.json').exists()


def test_unverified_archive_never_writes_payload(tmp_path):
    archive, _ = archive_at(tmp_path, [('Release/tool.exe', b'changed')])
    with pytest.raises(ValueError, match='checksum mismatch'):
        builder.extract_public_archive(archive, '0' * 64, tmp_path / 'payload', 'Release', {'tool.exe'})
    assert not (tmp_path / 'payload').exists()


@pytest.mark.parametrize('member', ['Release/../escape.txt', 'Release/C:/escape.txt',
                                   'Release/tools/../../escape.txt', 'Release/tools\\..\\..\\escape.txt'])
def test_archive_paths_are_validated_before_any_file_is_written(tmp_path, member):
    archive, checksum = archive_at(tmp_path, [('Release/tool.exe', b'public'), (member, b'bad')])
    with pytest.raises(ValueError):
        builder.extract_public_archive(archive, checksum, tmp_path / 'payload', 'Release', {'tool.exe', 'tools'})
    assert not (tmp_path / 'payload').exists()


def test_case_colliding_archive_entries_rejected_before_copy(tmp_path):
    archive, checksum = archive_at(tmp_path, [('Release/tool.exe', b'one'), ('Release/TOOL.exe', b'two')])
    with pytest.raises(ValueError, match='Duplicate'):
        builder.extract_public_archive(archive, checksum, tmp_path / 'payload', 'Release', {'tool.exe'})
    assert not (tmp_path / 'payload').exists()


@pytest.mark.parametrize('change', ['modify', 'extra'])
def test_resume_packaging_rejects_changed_or_unmanifested_data(tmp_path, change):
    stage = tmp_path / 'payload'; stage.mkdir()
    (stage / 'public.txt').write_text('public')
    prepared = {'version': builder.build.__version__, 'components': builder.COMPONENTS,
                'files': builder.inventory(stage)}
    (tmp_path / 'prepared-public-files.json').write_text(json.dumps(prepared), encoding='utf-8')
    if change == 'modify':
        (stage / 'public.txt').write_text('modified')
    else:
        (stage / 'config.json').write_text('{"token":"fixture-private"}')
    with pytest.raises(ValueError, match='changed|Unmanifested'):
        builder.package_prepared(tmp_path, tmp_path / 'unused-binaries')


def test_desktop_manifest_versions_include_current_public_tool_releases():
    assert builder.COMPONENTS['video-freeze'] == '1.1.1'
    assert builder.COMPONENTS['kling-batch'] == '1.2.1'
    assert builder.COMPONENTS['beat-workshop'] == '0.5.0'
    assert builder.COMPONENTS['color-workshop'] == '1.0.1'
    assert builder.COMPONENTS['desktop'] == builder.build.__version__


def test_legacy_builder_cannot_silently_downgrade_a_desktop_release(monkeypatch):
    monkeypatch.setattr(builder.build, '__version__', '1.1.0')
    with pytest.raises(SystemExit, match='build_desktop.py'):
        builder.build.main()


def test_directory_release_keeps_each_runtime_beside_compatible_entry(tmp_path, monkeypatch):
    from build_directory_release import RUNTIMES
    stage = tmp_path / 'payload'; stage.mkdir()
    (stage / 'public.txt').write_bytes(b'audited public tool')
    prepared = {'version': builder.build.__version__, 'components': builder.COMPONENTS,
                'files': builder.inventory(stage)}
    (tmp_path / 'prepared-public-files.json').write_text(json.dumps(prepared), encoding='utf-8')
    binaries = tmp_path / 'binaries'
    for name, runtime in RUNTIMES.items():
        source = binaries / Path(name).stem
        (source / runtime / 'nested').mkdir(parents=True)
        (source / name).write_bytes(b'MZ' + b'x' * 100_001)
        (source / runtime / 'nested/shared.dll').write_bytes(name.encode())
        (source / runtime / 'VCRUNTIME140.dll').write_bytes(b'system prerequisite')
        (source / runtime / 'api-ms-win-core-console-l1-1-0.dll').write_bytes(b'OS API contract')
        (source / runtime / 'mfc140u.dll').write_bytes(b'required app-local third-party library')
    core = tmp_path / 'InstallerCore'
    calls = []
    monkeypatch.setattr(builder.build, 'package', lambda *a, **kw: calls.append((a, kw)))
    builder.package_prepared(tmp_path, binaries, installer_core=core)
    assert len(calls) == 1 and calls[0][1]['native_core'] == core
    for name, runtime in RUNTIMES.items():
        assert (stage / name).read_bytes() == (binaries / Path(name).stem / name).read_bytes()
        assert (stage / runtime / 'nested/shared.dll').read_bytes() == name.encode()
        assert not (stage / runtime / 'VCRUNTIME140.dll').exists()
        assert not (stage / runtime / 'api-ms-win-core-console-l1-1-0.dll').exists()
        assert (stage / runtime / 'mfc140u.dll').read_bytes() == b'required app-local third-party library'
    assert (stage / 'public.txt').read_bytes() == b'audited public tool'


def test_directory_release_refuses_missing_runtime_before_packaging(tmp_path, monkeypatch):
    stage = tmp_path / 'payload'; stage.mkdir()
    (tmp_path / 'prepared-public-files.json').write_text(json.dumps({
        'version': builder.build.__version__, 'components': builder.COMPONENTS, 'files': []}), encoding='utf-8')
    source = tmp_path / 'binaries/CodexBridge'; source.mkdir(parents=True)
    (source / 'CodexBridge.exe').write_bytes(b'MZ' + b'x' * 100_001)
    monkeypatch.setattr(builder.build, 'package', lambda *a, **kw: pytest.fail('Incomplete runtime was packaged'))
    with pytest.raises(ValueError, match='Missing/unsafe directory runtime'):
        builder.package_prepared(tmp_path, tmp_path / 'binaries', installer_core=tmp_path / 'core')


def test_native_wrapper_build_metadata_is_not_shipped_in_installation(tmp_path, monkeypatch):
    import build_directory_release as native
    stage = tmp_path / 'payload'; stage.mkdir()
    (stage / 'public.txt').write_bytes(b'public product')
    core = tmp_path / 'core'; (core / 'runtime-installer').mkdir(parents=True)
    (core / 'InstallerCore.exe').write_bytes(b'MZ' + b'installer core')
    def compile_fixture(command):
        output = Path(command[command.index('--output') + 1])
        output.write_bytes(b'MZ' + b'complete wrapper')
        for suffix in ('.wrapper-payload.zip', '.manifest', '.wrapper-build.cs', '.application.manifest', '.wrapper-build.json'):
            output.with_suffix(suffix).write_text('private build metadata')
    monkeypatch.setattr(native, 'invoke', compile_fixture)
    release = native.package_native(stage, core, builder.COMPONENTS, tmp_path / 'release')
    manifest = json.loads(release.with_name('release.json').read_text(encoding='utf-8'))
    assert {item['path'] for item in manifest['files']} == {'public.txt', 'Uninstall.exe', '使用说明.md'}
    assert {p.name for p in release.parent.iterdir()} == {release.name, 'release.json', 'SHA256SUMS.txt'}
