"""Build the single-EXE per-user MSI launcher from an audited MSI/proof report."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

from build_native_wrapper import CSC, PREREQUISITE_SOURCE, RUNTIME_PROFILE, runtime_requirements, safe_name

SOURCE = Path(__file__).with_name('NativeMsiBootstrapper.cs')
ENTRIES = {'CodexBridge.exe', 'HubDesktop.exe', 'HubAgentRuntime.exe'}
OLE_HEADER = bytes.fromhex('d0cf11e0a1b11ae1')


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def regular_file(path: Path) -> Path:
    path = path.absolute()
    if not path.is_file() or path.is_symlink() or any(p.is_symlink() or p.is_junction() for p in path.parents):
        raise ValueError('Input must be a regular file outside reparse directories')
    return path


def guid(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'\{[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\}', value):
        raise ValueError('Invalid MSI product or component identifier')
    return value.upper()


def validate_proof(proof: dict, version: str) -> list[dict]:
    for flag in ('per_user_only', 'no_custom_actions', 'no_auto_start', 'no_legacy_cleanup'):
        if proof.get(flag) is not True:
            raise ValueError('MSI proof does not confirm required policy: ' + flag)
    if not re.fullmatch(r'\d{1,3}\.\d{1,3}\.\d{1,5}', version):
        raise ValueError('Expected an MSI major.minor.build version')
    parts = [int(v) for v in version.split('.')]
    if parts[0] > 255 or parts[1] > 255 or parts[2] > 65535:
        raise ValueError('MSI version is outside supported bounds')
    guid(proof['product_code'])
    guid(proof['package_code'])
    guid(proof['proof_component_code'])
    if proof['product_version'] != version or proof['install_dir_property'] != 'INSTALLDIR':
        raise ValueError('MSI proof version or install-directory contract differs')
    if proof['default_relative_install_dir'] != 'Programs/CodexHub-MSI/' + version:
        raise ValueError('MSI proof must use the separate per-user version directory')
    if proof['proof_relative_path'] != 'msi-payload-manifest.json':
        raise ValueError('Unexpected MSI payload marker')
    cache = 'BYBC/CodexHubInstallerCache/' + guid(proof['product_code'])[1:-1] + '/' + guid(proof['package_code'])[1:-1] + '.msi'
    if (proof.get('source_cache_relative_path') != cache
            or proof.get('cache_lifecycle') != 'retained-for-repair-and-rollback'
            or proof.get('cache_removed_by_msi') is not False):
        raise ValueError('MSI must retain its fixed product/package source cache')
    entries = proof['entrypoints']
    if len(entries) != 3 or {row['path'] for row in entries} != ENTRIES:
        raise ValueError('The proof must bind all three product entrypoints')
    files = [{'path': proof['proof_relative_path'], 'sha256': proof['proof_sha256']}, *entries]
    for row in files:
        safe_name(row['path'])
        if not re.fullmatch(r'[a-f0-9]{64}', row['sha256']):
            raise ValueError('Invalid payload checksum')
    if not re.fullmatch(r'[a-f0-9]{64}', proof['msi_sha256']):
        raise ValueError('Invalid MSI checksum')
    return files


def compile_bootstrapper(msi: Path, proof: dict, output: Path, *, version: str, icon: Path | None = None,
                         runtime_profile: Path = RUNTIME_PROFILE, extra_sources=(), main_class=None):
    """Compile frozen metadata; extra_sources/main_class are for non-installing test harnesses."""
    files = validate_proof(proof, version)
    if not CSC.is_file():
        raise RuntimeError('Windows .NET Framework C# compiler was not found')
    metadata = output.with_suffix('.msi-bootstrap-build.cs')
    string = lambda value: json.dumps(value, ensure_ascii=True)
    declarations = '\n'.join('internal const string ' + name + ' = ' + string(value) + ';' for name, value in {
        'Version': version, 'ProductCode': guid(proof['product_code']), 'ProofComponentCode': guid(proof['proof_component_code']),
        'DefaultDirectory': proof['default_relative_install_dir'], 'ProofPath': proof['proof_relative_path'],
        'MsiHash': proof['msi_sha256'],
        'CacheRelativePath': proof['source_cache_relative_path'],
    }.items())
    declarations += '\ninternal const long MsiBytes = ' + str(msi.stat().st_size) + 'L;\n'
    declarations += 'internal static readonly NativeMsiBootstrapper.PayloadFile[] VerifiedFiles = new NativeMsiBootstrapper.PayloadFile[]{' + ','.join(
        'new NativeMsiBootstrapper.PayloadFile{Path=' + string(row['path']) + ',Sha256=' + string(row['sha256']) + '}' for row in files) + '};\n'
    metadata.write_text('internal static class MsiBootstrapBuild {\n' + declarations + runtime_requirements(runtime_profile) + '}\n', encoding='utf-8')
    manifest = output.with_suffix('.application.manifest')
    manifest.write_text('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">'
        '<trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges>'
        '<requestedExecutionLevel level="asInvoker" uiAccess="false"/>'
        '</requestedPrivileges></security></trustInfo></assembly>', encoding='utf-8')
    command = [str(CSC), '/nologo', '/target:winexe' if main_class is None else '/target:exe', '/platform:x64', '/optimize+',
               '/reference:System.Windows.Forms.dll', '/reference:System.Web.Extensions.dll', '/out:' + str(output), '/win32manifest:' + str(manifest),
               '/resource:' + str(msi) + ',CodexHub.Package.msi', str(SOURCE), str(PREREQUISITE_SOURCE), str(metadata)]
    if icon is not None:
        command.append('/win32icon:' + str(icon))
    if main_class is not None:
        command.append('/main:' + main_class)
    command.extend(str(path) for path in extra_sources)
    subprocess.run(command, check=True, creationflags=subprocess.CREATE_NO_WINDOW)


def build(msi: Path, output: Path, *, proof_json: Path, version: str, icon: Path | None = None) -> dict:
    msi, proof_json = regular_file(msi), regular_file(proof_json)
    output = output.absolute()
    if output in {msi, proof_json} or output.suffix.lower() != '.exe':
        raise ValueError('Use a separate EXE output path')
    if output.exists() and (output.is_symlink() or output.is_junction()):
        raise ValueError('Output must not be a reparse point')
    if any(p.is_symlink() or p.is_junction() for p in output.parents):
        raise ValueError('Output directories must not be reparse points')
    if not 0 < msi.stat().st_size <= 2 * 1024**3:
        raise ValueError('MSI exceeds the supported package bound')
    with msi.open('rb') as stream:
        if stream.read(8) != OLE_HEADER:
            raise ValueError('Expected a Windows Installer compound-file MSI')
    proof = json.loads(proof_json.read_text(encoding='utf-8'))
    validate_proof(proof, version)
    if digest(msi) != proof['msi_sha256']:
        raise ValueError('MSI bytes differ from the independently built proof report')
    output.parent.mkdir(parents=True, exist_ok=True)
    compile_bootstrapper(msi, proof, output, version=version, icon=icon)
    if digest(msi) != proof['msi_sha256']:
        raise ValueError('MSI changed during bootstrapper compilation')
    report = {'version': version, 'installer': str(output), 'bytes': output.stat().st_size, 'sha256': digest(output),
              'msi_sha256': proof['msi_sha256'], 'product_code': guid(proof['product_code']),
              'proof_component_code': guid(proof['proof_component_code']), 'proof_sha256': proof['proof_sha256'],
              'source_cache_relative_path': proof['source_cache_relative_path'], 'cache_lifecycle': proof['cache_lifecycle'],
              'source_sha256': digest(SOURCE), 'builder_sha256': digest(Path(__file__)),
              'prerequisite_source_sha256': digest(PREREQUISITE_SOURCE), 'runtime_profile_sha256': digest(RUNTIME_PROFILE),
              'proof_report_sha256': digest(proof_json), 'requires_elevation': False,
              'msiexec_path_policy': 'fixed Windows System32', 'automatic_restart': False,
              'force_close_apps': False, 'legacy_python_cleanup': False}
    output.with_suffix('.msi-bootstrap-build.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--msi', type=Path, required=True)
    parser.add_argument('--proof-json', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--icon', type=Path)
    args = parser.parse_args()
    build(args.msi, args.output, proof_json=args.proof_json, version=args.version, icon=args.icon)


if __name__ == '__main__':
    main()
