"""Package audited public tool releases and separately built Hub executables.

Never source a release from user configuration, an installed application's
mutable runtime files, or a recursive copy of an unmanifested folder.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import shutil
import sys
import zipfile

import build

ROOT = build.ROOT
OUTPUT = ROOT.parent / 'output/hub-image-input-20261029'
COMPONENTS = {'bridge': build.__version__, 'agent-runtime': build.__version__, 'desktop': build.__version__,
              'video-freeze': '1.1.1', 'kling-batch': '1.2.1', 'beat-workshop': '0.5.0', 'color-workshop': '1.0.1'}
VIDEO_SHA256 = 'cdd3c65b18a8cd5684b2f36c4580960d8e5446920faea555c6d020fc5ce5ea71'
KLING_SHA256 = 'd5258d7ca24d9e16192f9074b9530512ba59b4c02b01b2f8e70f43ff9d4e5927'
KLING_EXE_SHA256 = '7eef249f52a3412dd55e37eb83fad1b6d4e05134ddde32ef5010fb57f93ccc69'
BINARIES = ('CodexBridge.exe', 'HubAgentRuntime.exe', 'HubDesktop.exe')


def safe_target(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if (not name or '\\' in name or ':' in name or relative.is_absolute()
            or any(part in ('', '.', '..') for part in name.split('/'))):
        raise ValueError('Invalid release path: ' + name)
    target = (root / name).resolve()
    if not target.is_relative_to(root.resolve()) or target == root.resolve():
        raise ValueError('Release path leaves staging directory: ' + name)
    return target


def extract_public_archive(archive: Path, checksum: str, destination: Path,
                           archive_root: str, allowed_roots: set[str]) -> None:
    if build.sha(archive) != checksum:
        raise ValueError('Release archive checksum mismatch: ' + archive.name)
    with zipfile.ZipFile(archive) as source:
        if source.testzip() is not None:
            raise ValueError('Release archive CRC failed: ' + archive.name)
        selected = []
        seen = set()
        for entry in source.infolist():
            if entry.is_dir():
                continue
            full = PurePosixPath(entry.filename)
            if len(full.parts) < 2 or full.parts[0] != archive_root:
                raise ValueError('Unexpected release archive root: ' + entry.filename)
            relative = '/'.join(full.parts[1:])
            target = safe_target(destination, relative)
            if relative.casefold() in seen or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Duplicate/link release member: ' + relative)
            seen.add(relative.casefold())
            if full.parts[1] in allowed_roots:
                selected.append((entry, target))
        if not selected:
            raise ValueError('Empty public release archive')
        for entry, target in selected:
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(entry) as incoming, target.open('wb') as outgoing:
                shutil.copyfileobj(incoming, outgoing)


def inventory(stage: Path) -> list[dict]:
    files = []
    for path in sorted(stage.rglob('*')):
        if not path.is_file():
            continue
        relative = path.relative_to(stage).as_posix()
        safe_target(stage, relative)
        files.append({'path': relative, 'bytes': path.stat().st_size, 'sha256': build.sha(path)})
    return files


def prepare(stage_root: Path, video_archive: Path, kling_archive: Path) -> None:
    stage = stage_root / 'payload'
    stage.mkdir(parents=True, exist_ok=False)
    build.copy_music(stage)
    build.copy_color(stage)
    extract_public_archive(video_archive, VIDEO_SHA256, stage / 'tools/video-freeze',
        'BYBC视频末帧延长工具', {'_internal', 'ffmpeg', 'licenses', 'BYBC视频末帧延长工具.exe'})
    extract_public_archive(kling_archive, KLING_SHA256, stage / 'tools/kling-batch', '可灵批处理1.2.1',
        {'可灵批处理.exe', '使用说明.md', '先读我.txt', '版本与校验.json', '源码与构建.zip', 'licenses'})
    if build.sha(stage / 'tools/kling-batch/可灵批处理.exe') != KLING_EXE_SHA256:
        raise ValueError('Kling executable checksum mismatch')
    prepared = {'version': build.__version__, 'components': COMPONENTS,
        'source_archives': [{'name': video_archive.name, 'sha256': VIDEO_SHA256},
                            {'name': kling_archive.name, 'sha256': KLING_SHA256}],
        'source_allowlists': [{'name': name, 'sha256': build.sha(Path(__file__).with_name(name))}
                             for name in ('music-files.json', 'color-files.json')],
        'files': inventory(stage)}
    (stage_root / 'prepared-public-files.json').write_text(json.dumps(prepared, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'prepared': str(stage), 'files': len(prepared['files']), 'components': COMPONENTS}, ensure_ascii=False))


def package_prepared(stage_root: Path, binaries_dir: Path) -> Path:
    stage = stage_root / 'payload'
    prepared = json.loads((stage_root / 'prepared-public-files.json').read_text(encoding='utf-8'))
    if prepared['version'] != build.__version__ or prepared['components'] != COMPONENTS:
        raise ValueError('Prepared payload targets a different release')
    expected = {item['path']: item for item in prepared['files']}
    actual = {item['path']: item for item in inventory(stage)}
    for name, item in expected.items():
        if actual.get(name) != item:
            raise ValueError('Prepared public file changed: ' + name)
    extras = set(actual) - set(expected) - set(BINARIES) - {'Uninstall.exe', 'bundle-manifest.json', '使用说明.md'}
    if extras:
        raise ValueError('Unmanifested files in staging: ' + ', '.join(sorted(extras)))
    executables = []
    for name in BINARIES:
        source = binaries_dir / name
        with source.open('rb') as incoming:
            if incoming.read(2) != b'MZ' or source.stat().st_size < 100_000:
                raise ValueError('Missing/invalid built executable: ' + name)
        before = build.sha(source)
        shutil.copy2(source, stage / name)
        if build.sha(source) != before or build.sha(stage / name) != before:
            raise ValueError('Executable changed during staging: ' + name)
        executables.append({'name': name, 'bytes': source.stat().st_size, 'sha256': before})
    (stage_root / 'native-inputs.json').write_text(json.dumps(executables, indent=2), encoding='utf-8')
    # The established installer retains upgrade, rollback, configuration
    # inheritance, verified-file cleanup and uninstall behavior unchanged.
    return build.package(stage, components=COMPONENTS, release_dir=stage_root / 'release')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage-root', type=Path, default=OUTPUT / ('package-v' + build.__version__))
    parser.add_argument('--binaries-dir', type=Path, default=ROOT / 'dist')
    parser.add_argument('--video-archive', type=Path,
        default=ROOT.parent / 'tools/bybc-video-freeze/release/BYBC_视频末帧延长工具_Portable_v1.1.1.zip')
    parser.add_argument('--kling-archive', type=Path)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--package-prepared', action='store_true')
    args = parser.parse_args()
    stage_root = args.stage_root.resolve()
    if not stage_root.is_relative_to(OUTPUT.resolve()) or stage_root == OUTPUT.resolve():
        parser.error('staging must be a dedicated child of ' + str(OUTPUT))
    if args.prepare_only and args.package_prepared:
        parser.error('--prepare-only and --package-prepared are mutually exclusive')
    if not args.package_prepared:
        if not args.kling_archive:
            parser.error('--kling-archive is required when preparing public files')
        prepare(stage_root, args.video_archive, args.kling_archive)
    if not args.prepare_only:
        package_prepared(stage_root, args.binaries_dir)


if __name__ == '__main__':
    main()
