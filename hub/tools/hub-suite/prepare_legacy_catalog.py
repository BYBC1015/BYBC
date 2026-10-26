"""Build credential-free fingerprints from audited releases, never user settings."""
from pathlib import Path
import hashlib
import json
import re

ROOT = Path(__file__).resolve().parents[2]
REPO = ROOT.parent
TARGET = ROOT / 'codex_bridge/assets/suite-legacy.json'

def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def main():
    catalog = json.loads(TARGET.read_text(encoding='utf-8')) if TARGET.exists() else {'entries':{}, 'manifests':[], 'files':{'kling':{}, 'music':{}, 'video':{}}}
    def entry(checksum, kind, version):
        catalog['entries'][checksum] = {'kind':kind,'version':version}
    def file(kind, name, checksum):
        values = catalog['files'][kind].setdefault(name, [])
        if checksum not in values: values.append(checksum)
    audit = REPO / 'output/hub-suite-685-v125/cleanup-plan.json'
    if audit.exists():
        for item in json.loads(audit.read_text(encoding='utf-8'))['files']:
            path, base = Path(item['path']), Path(item['root'])
            relative = path.relative_to(base).as_posix()
            checksum = item['sha256']
            if path.name == 'bundle-manifest.json' and checksum not in catalog['manifests']:
                catalog['manifests'].append(checksum)
            if item['category'] == 'old-bridge-release' and path.name.startswith('CodexBridge'):
                found = re.search(r'v(\d+\.\d+\.\d+)', path.name)
                entry(checksum, 'bridge', found.group(1) if found else '旧版')
            if item['category'] in {'standalone-kling','kling-license'}:
                file('kling', relative, checksum)
                if path.name == '可灵批处理.exe': entry(checksum, 'kling', base.name.split('_v')[-1])
            if item['category'] == 'standalone-music':
                file('music', relative, checksum)
                if relative == 'package.json': entry(checksum, 'music', '0.4及更早')
            if relative == 'CodexBridge.exe': entry(checksum, 'bridge', '1.0.24')
    previous = ROOT / 'build/hub-suite/payload/bundle-manifest.json'
    if previous.exists():
        manifest = json.loads(previous.read_text(encoding='utf-8'))
        checksum = digest(previous)
        if checksum not in catalog['manifests']: catalog['manifests'].append(checksum)
        for item in manifest['files']:
            rel, checksum = item['path'], item['sha256']
            if rel == 'CodexBridge.exe': entry(checksum, 'bridge', manifest['version'])
            for kind, prefix in [('kling','tools/kling-batch/'),('music','tools/beat-workshop/'),('video','tools/video-freeze/')]:
                if rel.startswith(prefix):
                    name = rel[len(prefix):]
                    file(kind, name, checksum)
                    if name in {'可灵批处理.exe','package.json','BYBC视频末帧延长工具.exe'}:
                        entry(checksum, kind, manifest['components'][{'kling':'kling-batch','music':'beat-workshop','video':'video-freeze'}[kind]])
    TARGET.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"Pinned {len(catalog['entries'])} legacy program fingerprints; no user data included.")

if __name__ == '__main__': main()
