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
