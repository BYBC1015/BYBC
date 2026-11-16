"""Public MSI authoring contracts; native fixture builds never install a product."""
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

TOOL_DIR = Path(__file__).resolve().parents[1] / 'tools/hub-suite'
sys.path.insert(0, str(TOOL_DIR))
import build_msi_release as builder


def payload_at(tmp_path, version='1.3.0'):
    payload = tmp_path / 'public 中文'
    payload.mkdir(parents=True)
    names = [*builder.ENTRYPOINTS, 'runtime-bridge/base_library.zip',
             'runtime-agent/base_library.zip', 'runtime-desktop/base_library.zip',
             'tools/kling-batch/源代码构建.zip', 'tools/beat-workshop/assets/fluidsynth/bin/library.dat',
             'tools/beat-workshop/assets/FluidSynth-LICENSE.txt', 'tools/beat-workshop/assets/sources.json',
             *builder.EXCLUDED]
    files = []
    for name in names:
        path = payload / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(('synthetic fixture ' + name).encode())
        files.append({'path': name, 'bytes': path.stat().st_size, 'sha256': builder.digest(path)})
    (payload / 'bundle-manifest.json').write_text(json.dumps({'version': version, 'files': files}), encoding='utf-8')
    return payload


def test_exact_public_plan_keeps_required_archives_and_excludes_only_declared_non_msi_files(tmp_path):
    payload = payload_at(tmp_path)
    plan = builder.public_plan(payload)
    names = {row['path'] for row in plan['files']}
    assert {'runtime-bridge/base_library.zip', 'tools/kling-batch/源代码构建.zip'}.issubset(names)
    assert not names.intersection(builder.EXCLUDED)
    assert {row['path'] for row in plan['excluded']} == set(builder.EXCLUDED)
    assert plan['default_relative_install_dir'] == 'Programs/CodexHub-MSI/1.3.0'


@pytest.mark.parametrize('change', ['modified', 'extra', 'duplicate', 'escape'])
def test_no_unverified_or_unmanifested_public_inputs(tmp_path, change):
    payload = payload_at(tmp_path)
    manifest = payload / 'bundle-manifest.json'
    document = json.loads(manifest.read_text())
    if change == 'modified': (payload / 'CodexBridge.exe').write_bytes(b'changed')
    elif change == 'extra': (payload / 'private-token.json').write_bytes(b'not public')
    elif change == 'duplicate': document['files'].append(document['files'][0])
    else: document['files'][0]['path'] = '../escape.exe'
    manifest.write_text(json.dumps(document))
    with pytest.raises(ValueError): builder.public_plan(payload)


@pytest.mark.parametrize('value', ['1.3.0.1', '1.3', '01.3.0', '256.0.0', '1.3.65536', '../1.3.0'])
def test_msi_versions_do_not_silently_ignore_a_fourth_field(value):
    with pytest.raises(ValueError): builder.valid_version(value)


def test_msi_ownership_upgrade_and_shortcut_contracts(tmp_path):
    plan = builder.public_plan(payload_at(tmp_path))
    tables = builder.table_plan(plan)
    properties = dict(tables['Property'])
    assert 'ALLUSERS' not in properties and 'MSIINSTALLPERUSER' not in properties
    assert properties['HUB_SHORTCUTS'] == '1'
    assert properties['MSIRESTARTMANAGERCONTROL'] == 'Disable'
    assert 'CustomAction' not in tables and 'ServiceInstall' not in tables
    assert len(tables['File']) == len(plan['files'])
    assert len(tables['Component']) == len(plan['files']) + 1
    assert all(row[2] is None and row[4] == 2 for row in tables['RemoveFile'])
    assert all(row[1] == 1 and row[2] == r'Software\BYBC\CodexHubMSI\1.3.0' for row in tables['Registry'])
    shortcut = tables['Shortcut'][0]
    entry = next(row for row in plan['files'] if row['path'] == 'CodexBridge.exe')
    assert shortcut[4] == '[#' + entry['file_id'] + ']' and shortcut[5] is None
    shortcut_component = next(row for row in tables['Component'] if row[0] == 'ShortcutComponent')
    assert shortcut_component[4] == 'HUB_SHORTCUTS=1'
    assert {row[0] for row in tables['Upgrade']} == {builder.UPGRADE_CODE}
    actions = {row[0]: row[2] for row in tables['InstallExecuteSequence']}
    assert actions['InstallExecute'] < actions['RemoveExistingProducts'] < actions['InstallFinalize']
    assert ['Installed OR NOT HUB_NEWER_PRODUCTS', 'A newer BYBC Hub MSI version is already installed.'] in tables['LaunchCondition']
    assert not any('VersionNT >= 1000' in row[0] for row in tables['LaunchCondition'])


def test_component_guids_are_stable_for_repair_and_distinct_between_versioned_roots(tmp_path):
    payload = payload_at(tmp_path)
    first = builder.public_plan(payload)
    again = builder.public_plan(payload)
    newer = builder.public_plan(payload, version='1.3.1')
    for plan in (first, again, newer): builder.table_plan(plan)
    assert first['product_code'] == again['product_code'] != newer['product_code']
    assert first['upgrade_code'] == newer['upgrade_code']
    assert [row['component_guid'] for row in first['files']] == [row['component_guid'] for row in again['files']]
    assert not ({row['component_guid'] for row in first['files']} & {row['component_guid'] for row in newer['files']})


def test_colliding_short_names_fail_before_cabinet_construction(tmp_path, monkeypatch):
    plan = builder.public_plan(payload_at(tmp_path))
    monkeypatch.setattr(builder, 'short_long', lambda name, unique: 'SAME|' + name)
    with pytest.raises(ValueError, match='Colliding MSI short filename'):
        builder.table_plan(plan)


def test_maintenance_cache_is_exact_package_specific_and_outside_msi_cleanup(tmp_path):
    product = '{C93D6D17-3A67-5DF4-ACB0-80A1E2A80052}'
    package = '{12345678-1234-4234-8234-123456789ABC}'
    contract = builder.maintenance_source_contract(product, package)
    assert contract == {
        'source_cache_relative_path': 'BYBC/CodexHubInstallerCache/C93D6D17-3A67-5DF4-ACB0-80A1E2A80052/12345678-1234-4234-8234-123456789ABC.msi',
        'cache_lifecycle': 'retained-for-repair-and-rollback', 'cache_removed_by_msi': False}
    other = builder.maintenance_source_contract(product, '{87654321-1234-4234-8234-123456789ABC}')
    assert other['source_cache_relative_path'] != contract['source_cache_relative_path']
    tables = builder.table_plan(builder.public_plan(payload_at(tmp_path)))
    assert all(row[2] is None for row in tables['RemoveFile'])
    assert not any('InstallerCache' in str(row) for rows in tables.values() for row in rows)
    with pytest.raises(ValueError):
        builder.maintenance_source_contract('../escape', package)


@pytest.mark.skipif(os.name != 'nt', reason='Windows database and cabinet APIs')
def test_native_database_and_embedded_cabinet_round_trip_without_installing(tmp_path):
    payload = payload_at(tmp_path)
    output = tmp_path / 'release/fixture.msi'
    report = builder.build_release(payload, output, work_directory=tmp_path / 'work 中文')
    assert report['msi_sha256'] == builder.digest(output)
    assert report['no_custom_actions'] and report['per_user_only'] and report['no_legacy_cleanup']
    assert report['file_count'] == len(builder.public_plan(payload)['files']) + 1
    assert report['proof_relative_path'] == 'msi-payload-manifest.json'
    assert report['cache_lifecycle'] == 'retained-for-repair-and-rollback'
    assert report['package_code'].strip('{}') + '.msi' in report['source_cache_relative_path']
    assert not report['cache_removed_by_msi']
    marker = json.loads((tmp_path / 'work 中文' / report['proof_relative_path']).read_text('utf-8'))
    assert marker['product_code'] == report['product_code']
    assert builder.PROOF_PATH not in {row['path'] for row in marker['files']}
    with builder.NativeMsi().database(output) as database:
        api = builder.NativeMsi()
        files = api.select(database, 'SELECT * FROM `File`')
        streams = api.select(database, 'SELECT `Name` FROM `_Streams`')
    assert any(row[2].endswith('|源代码构建.zip') for row in files)
    assert ['payload.cab'] in streams
    assert not (tmp_path / 'installed').exists()
