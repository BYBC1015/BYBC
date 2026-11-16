"""Only audited dependencies and one redundant cache leave new release payloads."""
import hashlib
import json
from pathlib import Path
import sys
import zipfile

import pytest

TOOL_DIR = Path(__file__).resolve().parents[1] / 'tools/hub-suite'
sys.path.insert(0, str(TOOL_DIR))
import build
import build_directory_release as native


def test_system_profile_matches_independently_audited_45_names():
    names = sorted(build.SYSTEM_RUNTIME_NAMES)
    assert len(names) == 45
    assert hashlib.sha256('\n'.join(names).encode()).hexdigest() == (
        '4b02cff3c02a4791151264704a5571f9f901733167eac509086855401a012149')


@pytest.mark.parametrize('scope', [
    'runtime-agent', 'runtime-bridge', 'runtime-desktop', 'runtime-installer',
    'tools/video-freeze/_internal',
])
def test_profile_removes_only_reviewed_names_inside_declared_scopes(tmp_path, scope):
    for name in build.SYSTEM_RUNTIME_NAMES:
        assert build.system_runtime_file(tmp_path / scope / name.upper(), tmp_path)
    for name in ('api-ms-win-future-l9-9-9.dll', 'ext-ms-private-contract.dll',
                 'mfc140u.dll', 'msvcp140.dll', 'vcruntime140_threads.dll', 'libcrypto-3.dll'):
        assert not build.system_runtime_file(tmp_path / scope / name, tmp_path)


@pytest.mark.parametrize('scope', [
    '', 'runtime-agent-extra', 'tools/video-freeze', 'tools/video-freeze/_internal-extra',
    'tools/beat-workshop/assets/fluidsynth/bin',
])
def test_even_reviewed_names_remain_outside_the_profile_scopes(tmp_path, scope):
    for name in build.SYSTEM_RUNTIME_NAMES:
        assert not build.system_runtime_file(tmp_path / scope / name, tmp_path)


def test_metadata_matches_all_actual_launcher_file_and_export_checks():
    requirements = json.loads((TOOL_DIR / 'windows-runtime-prerequisites.json').read_text(encoding='utf-8'))
    metadata = native.system_prerequisites_metadata()
    assert metadata['files'] == requirements['files']
    assert {item['file']: (len(item['required_named_exports']), len(item['required_ordinals']))
            for item in metadata['files']} == {
                'VCRUNTIME140.dll': (23, 0), 'VCRUNTIME140_1.dll': (1, 0), 'mfc140u.dll': (0, 1119)}
    assert metadata['system_directory_only'] and metadata['required_exports_check']
    assert metadata['existing_external_dependencies'] == ['mfc140u.dll']
    assert 'mfc140u.dll' not in metadata['system_profile_files']
    assert metadata['version_policy'] == requirements['version_policy']
    assert 'not a complete behavioral compatibility guarantee' in metadata['version_policy']
    assert 'vc_runtime_minimum' not in metadata


@pytest.mark.parametrize('packaging', ['directory', 'legacy'])
def test_new_payload_omits_only_download_cache_keeps_all_extracted_files_and_provenance(
        tmp_path, monkeypatch, packaging):
    stage = tmp_path / 'payload'
    stage.mkdir()
    music = json.loads((TOOL_DIR / 'music-files.json').read_text(encoding='utf-8'))
    members = [item['path'] for item in music['files'] if item['path'].startswith('assets/fluidsynth/')]
    assert len(members) == 28
    retained = ['tools/beat-workshop/' + path for path in members]
    retained += ['tools/beat-workshop/assets/FluidSynth-LICENSE.txt',
                 'tools/beat-workshop/assets/sources.json',
                 'tools/beat-workshop/assets/another.zip', 'other/fluidsynth.zip',
                 'runtime-agent/api-ms-win-future-l9-9-9.dll',
                 'runtime-agent/ext-ms-private-contract.dll', 'runtime-agent/mfc140u.dll']
    cache = 'tools/beat-workshop/assets/fluidsynth.zip'
    for relative in retained + [cache]:
        path = stage / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(('fixture: ' + relative).encode())
    cache_before = (stage / cache).read_bytes()
    old_manifest = tmp_path / 'old-version-owned.json'
    old_body = json.dumps({'files': [cache]}).encode()
    old_manifest.write_bytes(old_body)
    release_dir = tmp_path / 'release'
    components = {'bridge': build.__version__}

    if packaging == 'directory':
        core = tmp_path / 'core'
        (core / 'runtime-installer').mkdir(parents=True)
        (core / 'InstallerCore.exe').write_bytes(b'MZ synthetic installer fixture')

        def compile_fixture(command):
            output = Path(command[command.index('--output') + 1])
            output.write_bytes(b'MZ synthetic wrapper fixture')

        monkeypatch.setattr(native, 'invoke', compile_fixture)
        release = native.package_native(stage, core, components, release_dir)
    else:
        def compile_fixture(command, **kwargs):
            output = Path(command[command.index('--distpath') + 1])
            output.mkdir(parents=True, exist_ok=True)
            (output / (command[command.index('--name') + 1] + '.exe')).write_bytes(b'MZ fixture')

        monkeypatch.setattr(build.subprocess, 'run', compile_fixture)
        release = build.package(stage, components=components, release_dir=release_dir)

    manifest = json.loads(release.with_name('release.json').read_text(encoding='utf-8'))
    published = {item['path'] for item in manifest['files']}
    assert cache not in published
    assert set(retained).issubset(published)
    with zipfile.ZipFile(tmp_path / 'payload.zip') as archive:
        assert cache not in archive.namelist()
        for relative in retained:
            assert archive.read(relative) == (stage / relative).read_bytes()
    assert (stage / cache).read_bytes() == cache_before
    assert old_manifest.read_bytes() == old_body
