import importlib.util
import json
from pathlib import Path
import sys

import pytest

TOOLS = Path(__file__).resolve().parents[1] / 'tools/hub-suite'
sys.path.insert(0, str(TOOLS))
SPEC = importlib.util.spec_from_file_location('prepare_msi_payload_tested', TOOLS / 'prepare_msi_payload.py')
stage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stage)


def fixture(tmp_path):
    public = tmp_path / 'public'; public.mkdir()
    products = tmp_path / 'products'; products.mkdir()
    files = []
    for name, body in {'tools/source.zip': b'keep source', 'tools/beat-workshop/runtime.json': b'{}',
                       'tools/beat-workshop/assets/fluidsynth.zip': b'duplicate cache',
                       'Uninstall.exe': b'legacy uninstall', '使用说明.md': b'old installation guide'}.items():
        p = public / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(body)
        files.append({'path': name, 'bytes': len(body), 'sha256': stage.build.sha(p)})
    (public / 'bundle-manifest.json').write_text(json.dumps({'version': stage.build.__version__, 'components': stage.COMPONENTS, 'files': files}), encoding='utf-8')
    for name in stage.BINARIES:
        directory = products / Path(name).stem; directory.mkdir()
        (directory / name).write_bytes(b'MZ' + b'fixture' * 15000)
        runtime = directory / stage.RUNTIMES[name]; runtime.mkdir()
        (runtime / 'base_library.zip').write_bytes(b'required runtime')
        (runtime / 'VCRUNTIME140.dll').write_bytes(b'audited system prerequisite')
        (runtime / 'api-ms-future-unknown.dll').write_bytes(b'unknown must remain')
    guide = tmp_path / 'msi-guide.md'; guide.write_text('independent MSI directory', encoding='utf-8')
    return public, products, guide


def test_msi_staging_retains_required_archives_unknown_dlls_and_exact_new_products(tmp_path):
    public, products, guide = fixture(tmp_path)
    target = tmp_path / 'staged'
    result = stage.prepare(public, products, target, guide)
    names = {item['path'] for item in result['files']}
    assert not names & stage.EXCLUDED
    assert (target / 'tools/source.zip').read_bytes() == b'keep source'
    assert (target / '使用说明.md').read_bytes() == guide.read_bytes()
    for name in stage.BINARIES:
        assert (target / name).read_bytes() == (products / Path(name).stem / name).read_bytes()
        runtime = target / stage.RUNTIMES[name]
        assert (runtime / 'base_library.zip').read_bytes() == b'required runtime'
        assert (runtime / 'api-ms-future-unknown.dll').exists()
        assert not (runtime / 'VCRUNTIME140.dll').exists()
    for row in result['files']:
        assert stage.build.sha(target / row['path']) == row['sha256']


def test_even_excluded_public_input_must_match_audited_manifest(tmp_path):
    public, products, guide = fixture(tmp_path)
    (public / 'Uninstall.exe').write_bytes(b'changed')
    target = tmp_path / 'staged'
    with pytest.raises(ValueError, match='mismatch'):
        stage.prepare(public, products, target, guide)
    assert not target.exists()


def test_msi_staging_never_merges_with_existing_directory(tmp_path):
    public, products, guide = fixture(tmp_path)
    target = tmp_path / 'existing'; target.mkdir()
    (target / 'user.txt').write_bytes(b'user')
    with pytest.raises(ValueError, match='new independent'):
        stage.prepare(public, products, target, guide)
    assert (target / 'user.txt').read_bytes() == b'user'


def test_new_public_preparation_flows_directly_to_msi_without_legacy_installer(tmp_path):
    public, products, guide = fixture(tmp_path)
    source = public / 'bundle-manifest.json'
    source.rename(public.parent / 'prepared-public-files.json')
    result = stage.prepare(public, products, tmp_path / 'staged', guide)
    assert result['packaging'] == 'per-user-msi-side-by-side'
    assert (tmp_path / 'staged/tools/source.zip').read_bytes() == b'keep source'


def test_current_cli_never_silently_builds_historical_installer(tmp_path, monkeypatch):
    import build_desktop
    import build_directory_release
    monkeypatch.setattr(sys, 'argv', ['build_desktop.py', '--package-prepared'])
    with pytest.raises(SystemExit) as result: build_desktop.main()
    assert result.value.code == 2
    monkeypatch.setattr(sys, 'argv', ['build_directory_release.py', '--stage', 'core', '--stage-root', str(tmp_path / 'uncreated')])
    with pytest.raises(SystemExit) as result: build_directory_release.main()
    assert result.value.code == 2 and not (tmp_path / 'uncreated').exists()
    built = []
    monkeypatch.setattr(build_directory_release, 'build_binary', lambda name, target: built.append(name))
    monkeypatch.setattr(sys, 'argv', ['build_directory_release.py', '--stage', 'all', '--stage-root', str(tmp_path / 'modern')])
    build_directory_release.main()
    assert built == ['CodexBridge', 'HubAgentRuntime', 'HubDesktop']
