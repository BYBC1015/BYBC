"""Stage an exact public payload with fresh directory products for per-user MSI."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import stat

import build
from build_directory_release import RUNTIMES, system_prerequisites_metadata
from build_desktop import BINARIES, COMPONENTS, safe_target

EXCLUDED = {'Uninstall.exe', 'tools/beat-workshop/runtime.json', 'tools/beat-workshop/assets/fluidsynth.zip'}


def regular(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError('MSI input must be an ordinary file: ' + str(path))
    for parent in path.parents:
        if getattr(parent.lstat(), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError('MSI input cannot pass through a reparse point: ' + str(path))


def copy_verified(source: Path, target: Path, checksum: str) -> None:
    regular(source)
    if build.sha(source) != checksum:
        raise ValueError('Public input checksum changed: ' + str(source))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    if build.sha(target) != checksum or build.sha(source) != checksum:
        raise ValueError('Input changed while staging: ' + str(source))


def prepare(public_payload: Path, binaries_dir: Path, target: Path, readme: Path, *, public_manifest: Path | None = None) -> dict:
    manifest_path = public_manifest or public_payload / 'bundle-manifest.json'
    if public_manifest is None and not manifest_path.is_file():
        manifest_path = public_payload.parent / 'prepared-public-files.json'
    # Inspect lexical input chains before resolve() could hide a junction.
    for path in (manifest_path, readme):
        regular(path.absolute())
    public_payload, binaries_dir, target, readme = (path.resolve() for path in (public_payload, binaries_dir, target, readme))
    if target.exists() or any(target == root or target.is_relative_to(root) or root.is_relative_to(target) for root in (public_payload, binaries_dir)):
        raise ValueError('MSI staging requires a new independent directory')
    manifest_path = manifest_path.resolve()
    regular(manifest_path); regular(readme)
    original = json.loads(manifest_path.read_text(encoding='utf-8'))
    if original.get('version') != build.__version__ or original.get('components') != COMPONENTS:
        raise ValueError('Audited public payload has a different release identity')
    source_rows = original['files']
    seen = set()
    for item in source_rows:
        relative = item['path']
        key = relative.casefold()
        if key in seen: raise ValueError('Duplicate public input path')
        seen.add(key)
        safe_target(public_payload, relative)
        source = public_payload / relative
        regular(source)
        if source.stat().st_size != item['bytes'] or build.sha(source) != item['sha256']:
            raise ValueError('Audited public payload mismatch: ' + relative)
    target.mkdir(parents=True)
    for item in source_rows:
        relative = item['path']
        if relative in EXCLUDED | set(BINARIES) | {'使用说明.md'} or relative.split('/')[0] in set(RUNTIMES.values()):
            continue
        copy_verified(safe_target(public_payload, relative), safe_target(target, relative), item['sha256'])
    products = []
    for name in BINARIES:
        directory = binaries_dir / Path(name).stem
        exe = directory / name
        regular(exe)
        if exe.stat().st_size < 100_000 or exe.read_bytes()[:2] != b'MZ':
            raise ValueError('Missing directory product: ' + name)
        checksum = build.sha(exe)
        copy_verified(exe, target / name, checksum)
        products.append({'path': name, 'sha256': checksum, 'bytes': exe.stat().st_size})
        runtime = directory / RUNTIMES[name]
        if not runtime.is_dir() or runtime.is_symlink() or runtime.is_junction():
            raise ValueError('Missing private product runtime')
        for source in sorted(runtime.rglob('*')):
            if getattr(source.lstat(), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError('Runtime contains a reparse point')
            if not source.is_file(): continue
            relative = RUNTIMES[name] + '/' + source.relative_to(runtime).as_posix()
            destination = safe_target(target, relative)
            if build.system_runtime_file(destination, target): continue
            copy_verified(source, destination, build.sha(source))
    copy_verified(readme, target / '使用说明.md', build.sha(readme))
    files = [{'path': path.relative_to(target).as_posix(), 'bytes': path.stat().st_size, 'sha256': build.sha(path)}
             for path in sorted(target.rglob('*')) if path.is_file()]
    result = {'version': build.__version__, 'components': COMPONENTS, 'packaging': 'per-user-msi-side-by-side',
        'files': files, 'minimum_os': 'Windows 10 1803 x64 or newer',
        'system_prerequisites': system_prerequisites_metadata(),
        'source_manifest_sha256': build.sha(manifest_path), 'products': products,
        'unowned_user_preferences': ['tools/beat-workshop/runtime.json'],
        'excluded_installer_entries': ['Uninstall.exe'], 'legacy_installations_modified': False}
    (target / 'bundle-manifest.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public-payload', type=Path, required=True)
    parser.add_argument('--binaries-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--readme', type=Path, required=True)
    parser.add_argument('--public-manifest', type=Path,
        help='Explicit audited inventory; otherwise use bundle-manifest.json or the parent prepared-public-files.json')
    args = parser.parse_args()
    manifest = prepare(args.public_payload, args.binaries_dir, args.output, args.readme, public_manifest=args.public_manifest)
    print(json.dumps({'payload': str(args.output), 'files': len(manifest['files']), 'version': manifest['version']}))


if __name__ == '__main__': main()
