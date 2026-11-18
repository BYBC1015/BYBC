"""Reproducible directory binaries and native setup packaging for Windows."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import build

RUNTIMES = {'CodexBridge.exe': 'runtime-bridge', 'HubAgentRuntime.exe': 'runtime-agent',
            'HubDesktop.exe': 'runtime-desktop'}


def invoke(command):
    subprocess.run(command, cwd=build.ROOT, check=True,
                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def system_prerequisites_metadata():
    # Use the same file/export requirements compiled into the native launcher,
    # including its existing external MFC dependency and compatibility caveat.
    profile = json.loads(Path(__file__).with_name('windows-runtime-prerequisites.json').read_text(encoding='utf-8'))
    if profile.get('schema_version') != 1 or profile.get('architecture') != 'x64':
        raise ValueError('Unsupported runtime prerequisite metadata')
    return {'architecture': 'AMD64', 'system_directory_only': True,
            'required_exports_check': True, 'files': profile['files'],
            'version_policy': profile['version_policy'],
            'existing_external_dependencies': ['mfc140u.dll'],
            'system_profile_files': sorted(build.SYSTEM_RUNTIME_NAMES),
            'official_runtime_url': profile['official_x64_download'],
            'official_page': profile['official_page']}


def verify_desktop_assets(root):
    """Do not freeze a stale manifest while another task edits the web UI."""
    from codex_bridge.desktop_assets import build_asset_manifest
    static = Path(root) / 'codex_hub/static'
    try:
        declared = json.loads((static / 'desktop-assets.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('Regenerate desktop-assets.json after the UI source freeze before building HubDesktop') from exc
    if declared != build_asset_manifest(static, Path(root) / 'codex_hub/templates'):
        raise ValueError('Desktop asset manifest is stale; regenerate it after the UI source freeze')


def build_binary(name, stage_root):
    root = build.ROOT
    if name == 'HubDesktop':
        verify_desktop_assets(root)
    assets = root / 'codex_bridge/assets'
    core = name == 'InstallerCore'
    runtime = 'runtime-installer' if core else RUNTIMES[name + '.exe']
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir',
               '--contents-directory', runtime, '--name', name,
               '--console' if name == 'HubAgentRuntime' else '--windowed',
               '--paths', str(root), '--paths', str(Path(__file__).parent),
               '--distpath', str(stage_root / 'directory-dist'),
               '--workpath', str(stage_root / 'directory-build' / name),
               '--specpath', str(stage_root / 'directory-build')]
    if name != 'HubAgentRuntime': command += ['--icon', str(assets / 'codex-owl.ico')]
    if core:
        command += ['--hidden-import', 'win32com.client', '--add-data',
                    str(assets / 'suite-legacy.json') + ';codex_bridge/assets']
        entry = Path(__file__).with_name('native_installer_main.py')
    else:
        entry = root / 'tools' / {'CodexBridge': 'codex_bridge_main.py',
                  'HubAgentRuntime': 'agent_runtime_main.py', 'HubDesktop': 'hub_desktop_main.py'}[name]
        if name == 'CodexBridge': command += ['--collect-submodules', 'pystray']
        if name in ('CodexBridge', 'HubDesktop'):
            command += ['--add-data', str(assets) + ';codex_bridge/assets']
        if name == 'HubDesktop':
            command += ['--collect-all', 'webview', '--add-data', str(root / 'codex_hub/static') + ';codex_hub/static']
    if not entry.is_file(): raise ValueError('Missing frozen entry: ' + str(entry))
    command.append(str(entry))
    invoke(command)


def package_native(stage, core, components, release_dir):
    """Keep the audited install logic; replace only its one-file bootstrap."""
    stage, core, release_dir = Path(stage), Path(core), Path(release_dir)
    wrapper = Path(__file__).with_name('build_native_wrapper.py')
    if not (core / 'InstallerCore.exe').is_file(): raise ValueError('Missing directory installer core')
    if (core / 'runtime-installer/payload.zip').exists():
        raise ValueError('Uninstaller core must not contain the installation payload')
    filtered_core = stage.parent / 'core-system-dependencies'
    filtered_core.mkdir(exist_ok=False)
    system_files = []
    for directory, folders, filenames in os.walk(core, followlinks=False):
        parent = Path(directory)
        if any((parent / name).is_symlink() or (parent / name).is_junction() for name in folders + filenames):
            raise ValueError('Installer core contains a link')
        for name in filenames:
            source = parent / name
            relative = source.relative_to(core)
            if build.system_runtime_file(source, core):
                system_files.append({'path': relative.as_posix(), 'bytes': source.stat().st_size, 'sha256': build.sha(source)})
                continue
            target = filtered_core / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if build.sha(source) != build.sha(target): raise ValueError('Installer core changed during staging')
    (stage.parent / 'system-dependency-core-provenance.json').write_text(
        json.dumps(system_files, indent=2), encoding='utf-8')
    icon = build.ROOT / 'codex_bridge/assets/codex-owl.ico'
    def wrap(source, target):
        # Compiler inputs/reports can contain local paths and an extra full ZIP.
        # Only the final executable belongs in the installable/public payload.
        built = stage.parent / 'native-wrapper-build' / target.name
        built.parent.mkdir(parents=True, exist_ok=True)
        invoke([sys.executable, str(wrapper), '--payload-dir', str(source), '--entry', 'InstallerCore.exe',
                '--output', str(built), '--version', build.__version__, '--icon', str(icon)])
        before = build.sha(built)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(built, target)
        if build.sha(built) != before or build.sha(target) != before:
            raise ValueError('Native wrapper changed during publication staging')
    wrap(filtered_core, stage / 'Uninstall.exe')
    shutil.copy2(Path(__file__).with_name('README.md'), stage / '使用说明.md')
    files = [{'path': p.relative_to(stage).as_posix(), 'bytes': p.stat().st_size, 'sha256': build.sha(p)}
             for p in sorted(stage.rglob('*')) if p.is_file() and p.name != 'bundle-manifest.json'
             and not build.excluded_public_payload_file(p, stage)]
    manifest = {'version': build.__version__, 'components': components, 'files': files,
                'minimum_os': 'Windows 10 1803 x64 or newer', 'packaging': 'native-setup-directory-runtime',
                'system_runtime': 'Use Windows 10/11 API-set schema and system Universal C Runtime.',
                'system_prerequisites': system_prerequisites_metadata()}
    (stage / 'bundle-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    archive = stage.parent / 'payload.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as stream:
        for relative in [item['path'] for item in files] + ['bundle-manifest.json']:
            stream.write(stage / relative, relative)
    setup_core = stage.parent / 'setup-directory-core'
    shutil.copytree(filtered_core, setup_core)
    (setup_core / 'runtime-installer').mkdir(exist_ok=True)
    shutil.copy2(archive, setup_core / 'runtime-installer/payload.zip')
    release_dir.mkdir(parents=True, exist_ok=True)
    release = release_dir / ('CodexHub-Setup-v' + build.__version__ + '.exe')
    wrap(setup_core, release)
    manifest.update(installer=str(release), bytes=release.stat().st_size, sha256=build.sha(release))
    (release_dir / 'release.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    (release_dir / 'SHA256SUMS.txt').write_text(f"{manifest['sha256']}  {release.name}\n", encoding='utf-8')
    print(json.dumps({key: manifest[key] for key in ('installer', 'bytes', 'sha256', 'components')}))
    return release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=('workers', 'desktop', 'core', 'all'), required=True,
        help='all builds the three MSI product programs; historical core requires --legacy-custom-installer')
    parser.add_argument('--legacy-custom-installer', action='store_true',
        help='Explicitly include the historical custom InstallerCore; not used by the MSI release')
    parser.add_argument('--stage-root', type=Path, required=True)
    args = parser.parse_args()
    if args.stage == 'core' and not args.legacy_custom_installer:
        parser.error('InstallerCore 属于历史自定义安装器；标准 MSI 使用 build_msi_release.py。历史构建需 --legacy-custom-installer。')
    args.stage_root.mkdir(parents=True, exist_ok=True)
    (args.stage_root / 'directory-build').mkdir(exist_ok=True)
    if args.stage in ('workers', 'all'):
        for name in ('CodexBridge', 'HubAgentRuntime'): build_binary(name, args.stage_root)
    if args.stage in ('desktop', 'all'): build_binary('HubDesktop', args.stage_root)
    if args.stage == 'core' or (args.stage == 'all' and args.legacy_custom_installer):
        build_binary('InstallerCore', args.stage_root)


if __name__ == '__main__': main()
