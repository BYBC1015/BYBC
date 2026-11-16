"""Explicit, isolated Windows Installer engine acceptance; never runs by default.

Set HUB_MSI_ENGINE_QA_ROOT to a fresh directory under D:/CodexHub-QA and
HUB_MSI_ENGINE_ACCEPTANCE=1 only while holding the shared installation lane.
The fixture uses synthetic, non-executable payload bytes and unique identities.
"""
import ctypes
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools/hub-suite'))
import build_msi_release as builder


def assert_standard_action_log(log):
    assert not re.search(r'(?:Return value|返回值)\s+3(?:[.。]|\s|$)', log)
    for action in ('AppSearch', 'LaunchConditions', 'RemoveFiles'):
        assert re.search(re.escape(action) + r'[.。]\s*(?:Return value|返回值)\s+1[.。]', log)
    assert 'MainEngineThread is returning 0' in log


@pytest.mark.parametrize('localized', [False, True])
def test_standard_action_log_accepts_native_language_and_rejects_failure(localized):
    ending = '。返回值 1。' if localized else '. Return value 1.'
    log = '\n'.join(action + ending for action in ('AppSearch', 'LaunchConditions', 'RemoveFiles'))
    log += '\nMainEngineThread is returning 0'
    assert_standard_action_log(log)
    with pytest.raises(AssertionError):
        assert_standard_action_log(log.replace('1', '3', 1))


@pytest.mark.skipif(os.name != 'nt' or os.environ.get('HUB_MSI_ENGINE_ACCEPTANCE') != '1',
                    reason='Explicit authorization and an exclusive Windows Installer lane required')
def test_real_engine_install_repair_and_uninstall_in_private_fixture(monkeypatch):
    import winreg
    root = Path(os.environ['HUB_MSI_ENGINE_QA_ROOT']).absolute()
    allowed = Path('D:/CodexHub-QA').resolve()
    assert root.resolve().is_relative_to(allowed) and root.resolve() != allowed
    builder.reject_reparse(root)
    assert not root.exists(), 'Use a new fixture directory; never reuse an installed product.'
    root.mkdir(parents=True)
    # Isolate related-product discovery as well as ProductCode/components. The
    # production builder functions and standard action rows are used unchanged.
    namespace = uuid.uuid4()
    monkeypatch.setattr(builder, 'NAMESPACE', namespace)
    monkeypatch.setattr(builder, 'UPGRADE_CODE', builder.guid('engine-fixture-upgrade'))
    version = '0.0.' + str(10000 + namespace.int % 50000)
    payload = root / 'fixture 中文'
    payload.mkdir()
    files = []
    for relative in (*builder.ENTRYPOINTS, 'runtime-bridge/base_library.zip',
                     'tools/kling-batch/源代码构建.zip', 'tools/beat-workshop/fixture.txt'):
        source = payload / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(('Non-executable MSI schema fixture: ' + relative).encode('utf-8'))
        files.append({'path': relative, 'bytes': source.stat().st_size, 'sha256': builder.digest(source)})
    (payload / 'bundle-manifest.json').write_text(json.dumps({'version': version, 'files': files}), encoding='utf-8')
    msi = root / 'persistent source 中文' / 'fixture.msi'
    report = builder.build_release(payload, msi, work_directory=root / 'build')
    target = root / '安装 中文 空格'
    native = builder.NativeMsi()
    query = native.api.MsiQueryProductStateW
    query.argtypes, query.restype = [ctypes.c_wchar_p], ctypes.c_int
    product = report['product_code']
    registry_key = rf'Software\BYBC\CodexHubMSI\{version}'

    def registry_values():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry_key, 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                result = {}
                for index in range(winreg.QueryInfoKey(key)[1]):
                    name, value, _ = winreg.EnumValue(key, index)
                    result[name] = value
                return result
        except FileNotFoundError:
            return {}

    assert query(product) == -1 and not registry_values() and not target.exists()
    evidence = {'product_code': product, 'upgrade_code': report['upgrade_code'],
        'package_code': report['package_code'], 'version': version, 'qa_root': str(root),
        'msi_sha256': report['msi_sha256'], 'builder_sha256': builder.digest(builder.__file__),
        'target': str(target), 'phases': [], 'source_unchanged': False,
        'payload_executed': False, 'shortcut_enabled': False,
        'production_state_modified': False, 'security_settings_changed': False}
    live_process = None
    def save():
        (root / 'engine-verification.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')

    def run(phase, arguments):
        nonlocal live_process
        log = root / (phase + '.log')
        command = [str(native.system_directory / 'msiexec.exe'), *arguments,
                   '/qn', '/norestart', 'REBOOT=ReallySuppress',
                   'MSIRESTARTMANAGERCONTROL=Disable', 'HUB_SHORTCUTS=0', '/l*v', str(log)]
        # Native msiexec parses switches and property assignments itself; quote
        # values with whitespace, never an entire switch/property assignment.
        command_line = ' '.join(argument.split('=', 1)[0] + '=' + subprocess.list2cmdline([argument.split('=', 1)[1]])
            if re.match(r'^[A-Z_]+=', argument) else subprocess.list2cmdline([argument]) for argument in command)
        started = time.monotonic()
        live_process = subprocess.Popen(command_line, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        phase_result = {'phase': phase, 'command': command, 'command_line': command_line,
                        'pid': live_process.pid, 'log': str(log)}
        evidence['phases'].append(phase_result)
        save()
        try:
            stdout, stderr = live_process.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            phase_result.update(timed_out=True, seconds=time.monotonic() - started)
            save()
            raise  # Leave an active transaction untouched and report its PID.
        phase_result.update(exit_code=live_process.returncode, seconds=time.monotonic() - started,
                            stdout=stdout.decode(errors='replace'), stderr=stderr.decode(errors='replace'))
        save()
        assert live_process.returncode == 0, (phase, live_process.returncode, str(log))

    def owned_hashes_match():
        return all((target / row['path']).is_file() and builder.digest(target / row['path']) == row['sha256']
                   for row in report['files'])

    extras = {'personal-note.txt': b'fixture user content',
              'tools/beat-workshop/runtime.json': b'{"fixture_user_setting":true}'}
    try:
        run('install', ['/i', str(msi), 'INSTALLDIR=' + str(target)])
        assert query(product) == 5 and owned_hashes_match()
        assert Path(registry_values()['InstallDir']).resolve() == target.resolve()
        evidence['installed_product_state'] = query(product)
        evidence['install_owned_hashes_match'] = True
        for relative, value in extras.items():
            extra = target / relative
            extra.parent.mkdir(parents=True, exist_ok=True)
            extra.write_bytes(value)
        # Only remove this fixture-owned, hash-verified non-executable file.
        missing = target / 'runtime-bridge/base_library.zip'
        assert missing.resolve().is_relative_to(root.resolve())
        assert builder.digest(missing) == next(row['sha256'] for row in report['files'] if row['path'] == 'runtime-bridge/base_library.zip')
        missing.unlink()
        run('repair', ['/famus', product])
        assert query(product) == 5 and owned_hashes_match()
        assert all((target / relative).read_bytes() == value for relative, value in extras.items())
        evidence['repair_owned_hashes_match'] = True
        evidence['repair_used_product_code_without_installdir'] = True
        evidence['repair_user_files_retained'] = True
    finally:
        if live_process is None or live_process.poll() is not None:
            if query(product) != -1:
                run('uninstall', ['/x', product])
            evidence['final_product_state'] = query(product)
            evidence['final_registry_values'] = registry_values()
            evidence['owned_files_remaining'] = [row['path'] for row in report['files'] if (target / row['path']).exists()]
            evidence['source_unchanged'] = builder.digest(msi) == report['msi_sha256']
            evidence['user_files_retained_after_uninstall'] = all((target / relative).read_bytes() == value
                for relative, value in extras.items()) if evidence.get('repair_user_files_retained') else None
        else:
            evidence['active_transaction_pid'] = live_process.pid
        save()
    assert evidence['final_product_state'] == -1 and not evidence['final_registry_values']
    assert not evidence['owned_files_remaining'] and evidence['source_unchanged']
    assert evidence['user_files_retained_after_uninstall']
    for phase in ('install', 'repair', 'uninstall'):
        log = (root / (phase + '.log')).read_text(encoding='utf-16', errors='replace')
        assert_standard_action_log(log)
    evidence['passed'] = True
    save()
