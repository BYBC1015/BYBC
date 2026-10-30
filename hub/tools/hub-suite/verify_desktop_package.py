"""Verify the frozen installer in isolated directories, never the live install."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

from build_desktop import BINARIES, COMPONENTS, OUTPUT, safe_target
from installer import LOCAL_PREFERENCES


def run_installer(exe: Path, args: list[str], environment: dict, log: Path) -> None:
    with log.open('wb') as output:
        outcome = subprocess.run([str(exe), *args], env=environment, cwd=Path(environment['LOCALAPPDATA']).parent,
            stdout=output, stderr=subprocess.STDOUT, timeout=600,
            creationflags=subprocess.CREATE_NO_WINDOW)
    if outcome.returncode:
        raise RuntimeError(f'Isolated installer failed ({outcome.returncode}); see {log.name}')


def verify(release: Path, validation: Path) -> dict:
    release, validation = release.resolve(), validation.resolve()
    if not validation.is_relative_to(OUTPUT.resolve()) or validation == OUTPUT.resolve():
        raise ValueError('Validation must stay in a dedicated desktop release output directory')
    validation.mkdir(parents=True, exist_ok=False)
    environment = os.environ.copy()
    # Keep all native-process writes under a fresh Windows temp root. Reports
    # stay in the workspace, whose inherited execution ACL may differ from a
    # normal user's installation location. No existing/system ACL is changed.
    temporary = Path(tempfile.gettempdir()) / ('hub-installer-qa-' + uuid.uuid4().hex)
    temporary.mkdir(exist_ok=False)
    state_root = temporary / 'state'
    state_root.mkdir()
    for key, folder in [('APPDATA', 'Roaming'), ('LOCALAPPDATA', 'Local'), ('PROGRAMDATA', 'ProgramData')]:
        path = state_root / folder
        path.mkdir(exist_ok=True)
        environment[key] = str(path)
    # Use a dedicated directory under the Windows user's real temp location.
    # The one-file bootloader applies its own temporary-directory ACL; an
    # inherited workspace restriction can prevent extraction before Python.
    unpack = temporary / 'unpack'
    unpack.mkdir()
    environment['TEMP'] = environment['TMP'] = str(unpack)
    (validation / 'temporary-directory.txt').write_text(str(temporary), encoding='utf-8')
    environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    config = state_root / 'Roaming/CodexBridge/config.json'
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({'hub': 'https://isolated.example.invalid/hub',
        'token': 'isolated-verification-not-a-live-credential', 'projects': {}}), encoding='utf-8')
    config_before = config.read_bytes()
    install_root = state_root / '安装 中文 空格'
    flags = ['--quiet', '--install-dir', str(install_root), '--no-shortcuts', '--no-registration',
             '--no-startup', '--no-system-scan']
    run_installer(release, flags, environment, validation / 'install.log')
    manifest = json.loads((install_root / 'bundle-manifest.json').read_text(encoding='utf-8'))
    if manifest['components'] != COMPONENTS:
        raise AssertionError('Installed component versions differ from the audited package')
    expected = {item['path']: item for item in manifest['files']}
    for item in manifest['files']:
        path = safe_target(install_root, item['path'])
        if path.stat().st_size != item['bytes'] or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
            raise AssertionError('Installed hash mismatch: ' + item['path'])
    if not set(BINARIES).issubset(expected):
        raise AssertionError('Desktop/Bridge/runtime companion is missing')
    if config.read_bytes() != config_before:
        raise AssertionError('Existing isolated configuration was changed')
    state = json.loads((install_root / '.hub-suite-install.json').read_text(encoding='utf-8'))
    if state['startup'] or state['registration'] or state['shortcuts']:
        raise AssertionError('Isolation flags were not respected')

    personal = install_root / '个人工程/保留.txt'
    personal.parent.mkdir()
    personal.write_text('用户工作必须保留', encoding='utf-8')
    local_preference = install_root / 'tools/beat-workshop/runtime.json'
    local_preference.parent.mkdir(parents=True, exist_ok=True)
    local_preference.write_bytes(b'{"resolveRoot":"D:/Isolated Resolve","test":"keep exact bytes"}\r\n')
    preference_hash = hashlib.sha256(local_preference.read_bytes()).hexdigest()
    run_installer(release, flags, environment, validation / 'upgrade.log')
    upgraded = json.loads((install_root / '.hub-suite-install.json').read_text(encoding='utf-8'))
    preserved = {'tools/beat-workshop/runtime.json': preference_hash}
    if upgraded.get('local_preferences') != preserved:
        raise AssertionError('Upgrade lost local preference provenance')
    if json.loads((install_root / 'bundle-manifest.json').read_text(encoding='utf-8')) != manifest:
        raise AssertionError('Local preferences changed the pristine release manifest')
    for relative, item in expected.items():
        path = safe_target(install_root, relative)
        expected_hash = preserved.get(relative, item['sha256'])
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
            raise AssertionError('Upgraded hash mismatch: ' + relative)
    if personal.read_text(encoding='utf-8') != '用户工作必须保留' or config.read_bytes() != config_before:
        raise AssertionError('Upgrade lost an isolated user file/configuration')
    previous = Path(upgraded['previous_installation']).resolve()
    if not previous.is_relative_to(state_root.resolve()) or not previous.exists():
        raise AssertionError('Upgrade rollback directory not recorded inside isolation')
    if upgraded['cleanup'] and any(row['status'].startswith('失败') for row in upgraded['cleanup']):
        raise AssertionError('Verified old-file cleanup failed')

    # Exercise the actual frozen uninstaller from outside the target directory,
    # as its normal self-copying launcher does before removing owned files.
    helper = state_root / 'isolated-uninstall.exe'
    shutil.copy2(install_root / 'Uninstall.exe', helper)
    run_installer(helper, ['--remove', str(install_root), '--quiet'], environment, validation / 'uninstall.log')
    if any((install_root / name).exists() for name in expected if name not in LOCAL_PREFERENCES):
        raise AssertionError('Uninstall left installed owned files')
    if hashlib.sha256(local_preference.read_bytes()).hexdigest() != preference_hash:
        raise AssertionError('Uninstall changed an explicit local preference')
    if not personal.exists() or config.read_bytes() != config_before:
        raise AssertionError('Uninstall removed isolated user data/configuration')
    report = {'passed': True, 'installer': str(release), 'installer_bytes': release.stat().st_size,
        'installer_sha256': hashlib.sha256(release.read_bytes()).hexdigest(),
        'components': manifest['components'], 'verified_files': len(expected),
        'temporary_directory': str(temporary),
        'isolated_state_directory': str(state_root),
        'preserved_local_preferences': preserved,
        'checks': ['fresh install in Chinese/spaced directory', 'all installed hashes',
                   'desktop/bridge/runtime companions', 'same-version upgrade and rollback record',
                   'existing configuration byte preservation', 'user file retention',
                   'explicit local preference bytes retained through upgrade/uninstall',
                   'frozen uninstaller owned-file cleanup', 'no registration/shortcuts/startup/system scan'],
        'live_installation_modified': False, 'paid_api_calls': 0}
    (validation / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('installer', type=Path)
    parser.add_argument('--validation-dir', type=Path, required=True)
    args = parser.parse_args()
    verify(args.installer, args.validation_dir)


if __name__ == '__main__':
    main()
