"""Build a .NET Framework SFX around a frozen onedir installer, without downloads."""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import zipfile

HERE = Path(__file__).resolve().parent
CS_SOURCE = HERE / 'NativeInstallerWrapper.cs'
PREREQUISITE_SOURCE = HERE / 'WindowsRuntimePrerequisites.cs'
RUNTIME_PROFILE = HERE / 'windows-runtime-prerequisites.json'
CSC = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'

def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def safe_name(name):
    parts = name.split('/')
    if not name or '\\' in name or ':' in name or any(not part or part in {'.', '..'} or part[-1] in ' .' for part in parts):
        raise ValueError('Unsafe archive path')
    for part in parts:
        if any(ord(ch) < 32 or ch in '<>:"|?*' for ch in part):
            raise ValueError('Invalid Windows path')
        stem = part.split('.')[0].upper()
        if stem in {'CON', 'PRN', 'AUX', 'NUL'} or (len(stem) == 4 and stem[:3] in {'COM', 'LPT'} and stem[3] in '123456789'):
            raise ValueError('Reserved Windows path')

def is_reparse(path):
    return bool(getattr(path.lstat(), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)

def inventory(root):
    if any(is_reparse(p) for p in (root, *root.parents)):
        raise ValueError('Payload root may not traverse a reparse point')
    files = []
    seen = set()
    for directory, dirs, names in os.walk(root, followlinks=False):
        for item in [*dirs, *names]:
            path = Path(directory) / item
            if is_reparse(path) or path.is_symlink():
                raise ValueError('Payload contains a reparse point')
        for name in names:
            path = Path(directory) / name
            if not path.is_file():
                raise ValueError('Payload contains a special file')
            relative = path.relative_to(root).as_posix()
            safe_name(relative)
            if relative.casefold() in seen:
                raise ValueError('Case-insensitive duplicate payload path')
            seen.add(relative.casefold())
            files.append({'path': relative, 'bytes': path.stat().st_size, 'sha256': digest(path)})
    return sorted(files, key=lambda row: row['path'])

def runtime_requirements(profile):
    document = json.loads(Path(profile).read_text(encoding='utf-8'))
    if document.get('schema_version') != 1 or document.get('architecture') != 'x64' or document.get('minimum_windows_major') != 10:
        raise ValueError('Unsupported runtime prerequisite profile')
    entries = document['files']
    if {item['file'] for item in entries} != {'VCRUNTIME140.dll', 'VCRUNTIME140_1.dll', 'mfc140u.dll'} or len(entries) != 3:
        raise ValueError('Unexpected system prerequisite files')
    declarations = []
    for item in entries:
        version = [int(part) for part in item['minimum_file_version'].split('.')]
        if len(version) != 4 or any(not 0 <= part <= 65535 for part in version):
            raise ValueError('Invalid prerequisite version')
        names = item['required_named_exports']
        ordinals = item['required_ordinals']
        if len(names) > 10000 or len(ordinals) > 65535 or len(set(names)) != len(names) or len(set(ordinals)) != len(ordinals):
            raise ValueError('Invalid prerequisite export count')
        if any(not isinstance(name, str) or not name or len(name) > 4096 or any(ord(ch) < 32 or ord(ch) > 126 for ch in name) for name in names):
            raise ValueError('Invalid prerequisite export name')
        if any(type(value) is not int or not 0 < value <= 65535 for value in ordinals):
            raise ValueError('Invalid prerequisite ordinal')
        declarations.append('new WindowsRuntimePrerequisites.Requirement { FileName=' + json.dumps(item['file']) +
            ', MinimumVersion=new System.Version(' + ','.join(str(part) for part in version) + '), Names=new string[]{' +
            ','.join(json.dumps(name) for name in names) + '}, Ordinals=new uint[]{' +
            ','.join(str(value) + 'u' for value in ordinals) + '} }')
    return 'internal static readonly WindowsRuntimePrerequisites.Requirement[] RuntimeRequirements = new WindowsRuntimePrerequisites.Requirement[]{\n' + ',\n'.join(declarations) + '\n};\n'

def compile_wrapper(archive, manifest, entry, output, *, icon=None, source=CS_SOURCE, runtime_profile=RUNTIME_PROFILE):
    """Low-level entry also used by malformed-resource tests; no extraction here."""
    safe_name(entry)
    if not CSC.is_file():
        raise RuntimeError('Windows .NET Framework C# compiler was not found')
    records = manifest.read_text(encoding='utf-8').splitlines()
    total_bytes = sum(int(row.split('\t')[1]) for row in records)
    if not 0 < len(records) <= 100000 or not 0 <= total_bytes <= 16 * 1024**3:
        raise ValueError('Wrapper payload exceeds build bounds')
    metadata = output.parent / (output.stem + '.wrapper-build.cs')
    metadata.write_text('internal static class WrapperBuild {\n' +
        'internal const string Entry = ' + json.dumps(entry, ensure_ascii=True) + ';\n' +
        'internal const int FileCount = ' + str(len(records)) + ';\n' +
        'internal const long TotalBytes = ' + str(total_bytes) + 'L;\n' +
        'internal const string ZipHash = "' + digest(archive) + '";\n' +
        'internal const string ManifestHash = "' + digest(manifest) + '";\n' +
        runtime_requirements(runtime_profile) + '}\n', encoding='utf-8')
    app_manifest = output.parent / (output.stem + '.application.manifest')
    app_manifest.write_text('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">'
        '<trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges>'
        '<requestedExecutionLevel level="asInvoker" uiAccess="false"/>'
        '</requestedPrivileges></security></trustInfo></assembly>', encoding='utf-8')
    command = [str(CSC), '/nologo', '/target:winexe', '/platform:x64', '/optimize+',
        '/reference:System.IO.Compression.dll', '/reference:System.IO.Compression.FileSystem.dll',
        '/reference:System.Windows.Forms.dll', '/out:' + str(output),
        '/reference:System.Web.Extensions.dll',
        '/win32manifest:' + str(app_manifest),
        '/resource:' + str(archive) + ',CodexHub.Installer.zip',
        '/resource:' + str(manifest) + ',CodexHub.Installer.manifest', str(source), str(PREREQUISITE_SOURCE), str(metadata)]
    if icon:
        command.insert(-2, '/win32icon:' + str(icon))
    subprocess.run(command, check=True, creationflags=subprocess.CREATE_NO_WINDOW)

def build(payload, entry, output, *, version, icon=None):
    payload = payload.absolute()
    output = output.absolute()
    safe_name(entry)
    if output.is_relative_to(payload):
        raise ValueError('Wrapper output must be outside its input payload')
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = inventory(payload)
    if entry not in {row['path'] for row in rows}:
        raise ValueError('Entry is not present in payload')
    archive = output.parent / (output.stem + '.wrapper-payload.zip')
    manifest = output.parent / (output.stem + '.wrapper-payload.manifest')
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as destination:
        for row in rows:
            info = zipfile.ZipInfo(row['path'], date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            with (payload / row['path']).open('rb') as incoming, destination.open(info, 'w', force_zip64=True) as outgoing:
                import shutil
                shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            if digest(payload / row['path']) != row['sha256']:
                raise ValueError('Payload changed while packaging')
    manifest.write_text(''.join(row['sha256'] + '\t' + str(row['bytes']) + '\t' +
        base64.b64encode(row['path'].encode('utf-8')).decode('ascii') + '\n' for row in rows), encoding='utf-8')
    compile_wrapper(archive, manifest, entry, output, icon=icon)
    report = {'version': version, 'entry': entry, 'payload_root': str(payload), 'output': str(output),
        'output_bytes': output.stat().st_size, 'output_sha256': digest(output),
        'source_sha256': digest(CS_SOURCE), 'builder_sha256': digest(Path(__file__)),
        'prerequisite_source_sha256': digest(PREREQUISITE_SOURCE), 'runtime_profile_sha256': digest(RUNTIME_PROFILE),
        'archive_sha256': digest(archive), 'manifest_sha256': digest(manifest), 'files': rows,
        'requires_elevation': False, 'product_sources_changed': False}
    output.with_suffix('.wrapper-build.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key != 'files'}, ensure_ascii=True))
    return report

build_wrapper = build

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload-dir', type=Path, required=True)
    parser.add_argument('--entry', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--icon', type=Path)
    args = parser.parse_args()
    build(args.payload_dir, args.entry, args.output, version=args.version, icon=args.icon)

if __name__ == '__main__':
    main()
