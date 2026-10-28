"""Build a single installer from explicit, credential-free release artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from codex_bridge import __version__

KLING_SHA256 = "92cd829331cf6e0c4bd3ceb64a5461ebc1f5fa52eca40c76bfc15fe594e99657"


def system_runtime_file(path, stage):
    relative = path.relative_to(stage).as_posix()
    return relative.startswith("tools/video-freeze/_internal/") and (
        path.name.lower() == "ucrtbase.dll" or path.name.lower().startswith("api-ms-win-crt-"))


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kling-dir", type=Path, required=True)
    parser.add_argument("--reuse-payload", action="store_true", help="Resume packaging an already verified payload")
    args = parser.parse_args()
    subprocess.run([sys.executable, str(Path(__file__).with_name('prepare_legacy_catalog.py'))], check=True)
    video_release = ROOT.parent / "tools/bybc-video-freeze/release"
    archive = video_release / "BYBC_视频末帧延长工具_Portable_v1.0.1.zip"
    expected = next(line.split()[0] for line in (video_release / "SHA256SUMS.txt").read_text(encoding="utf-8-sig").splitlines() if line.endswith(archive.name))
    assert sha(archive) == expected, "Video release checksum mismatch"
    kling = args.kling_dir / "可灵批处理.exe"
    assert sha(kling) == KLING_SHA256, "Expected the verified Kling 1.1.1 release"
    stage = ROOT / "build/hub-suite/payload"
    if args.reuse_payload:
        saved = json.loads((stage / "bundle-manifest.json").read_text(encoding="utf-8"))
        assert all(sha(stage / f["path"]) == f["sha256"] for f in saved["files"])
        copy_music(stage)
        copy_color(stage)
        build_bridge(stage)
        package(stage)
        return
    stage.mkdir(parents=True, exist_ok=False)
    copy_music(stage)
    copy_color(stage)
    build_bridge(stage)
    video = stage / "tools/video-freeze"
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        for entry in z.infolist():
            parts = Path(entry.filename).parts
            assert parts[0] == "BYBC视频末帧延长工具" and ".." not in parts
            if entry.is_dir():
                continue
            relative = Path(*parts[1:])
            assert relative.parts[0] in {"_internal", "ffmpeg", "licenses", "BYBC视频末帧延长工具.exe"}
            target = video / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(entry))
    kling_target = stage / "tools/kling-batch"
    kling_target.mkdir(parents=True)
    for name in ("可灵批处理.exe", "使用说明.md", "先读我.txt"):
        shutil.copy2(args.kling_dir / name, kling_target / name)
    shutil.copy2(Path(__file__).with_name("README.md"), stage / "使用说明.md")
    files = [{"path": p.relative_to(stage).as_posix(), "bytes": p.stat().st_size, "sha256": sha(p)}
             for p in sorted(stage.rglob("*")) if p.is_file()]
    manifest = {"version": __version__, "components": {"bridge": __version__, "video-freeze": "1.0.1", "kling-batch": "1.1.1"}, "files": files}
    (stage / "bundle-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    package(stage)


def build_bridge(stage):
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
        "--collect-submodules", "pystray", "--name", "CodexBridge", "--icon", str(ROOT / "codex_bridge/assets/codex-owl.ico"),
        "--add-data", str(ROOT / "codex_bridge/assets") + ";codex_bridge/assets",
        "--paths", str(ROOT), "--distpath", str(stage), "--workpath", str(stage.parent / "pyinstaller"),
        "--specpath", str(stage.parent), str(ROOT / "tools/codex_bridge_main.py"),
    ], cwd=ROOT, check=True)
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--console",
        "--name", "HubAgentRuntime", "--paths", str(ROOT), "--distpath", str(stage),
        "--workpath", str(stage.parent / "agent-runtime"), "--specpath", str(stage.parent),
        str(ROOT / "tools/agent_runtime_main.py"),
    ], cwd=ROOT, check=True)


def copy_music(stage):
    source = ROOT.parent / "tools/davinci-beat-workshop"
    pinned = json.loads(Path(__file__).with_name("music-files.json").read_text(encoding="utf-8"))
    assert pinned["version"] == "0.5.0"
    assert json.loads((source / "package.json").read_text(encoding="utf-8"))["version"] == pinned["version"]
    destination = stage / "tools/beat-workshop"
    for item in pinned["files"]:
        relative = Path(item["path"])
        assert not relative.is_absolute() and ".." not in relative.parts
        assert sha(source / relative) == item["sha256"], f"Music source changed: {relative}"
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)
    # Only the discovered local Resolve directory is written here at first launch.
    (destination / "runtime.json").write_text("{}", encoding="utf-8")


def copy_color(stage):
    source = ROOT.parent / "tools/davinci-color-workshop"
    pinned = json.loads(Path(__file__).with_name("color-files.json").read_text(encoding="utf-8"))
    assert json.loads((source / "package.json").read_text(encoding="utf-8"))["version"] == pinned["version"]
    for item in pinned["files"]:
        relative = Path(item["path"])
        assert not relative.is_absolute() and ".." not in relative.parts
        assert sha(source / relative) == item["sha256"], f"Color source changed: {relative}"
        target = stage / "tools/color-workshop" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)


def package(stage):
    installer = Path(__file__).with_name("installer.py")
    common = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
              "--icon", str(ROOT / "codex_bridge/assets/codex-owl.ico"), "--hidden-import", "win32com.client",
              "--add-data", str(ROOT / "codex_bridge/assets/suite-legacy.json") + ";codex_bridge/assets",
              "--paths", str(ROOT),
              "--specpath", str(stage.parent)]
    subprocess.run(common + ["--name", "Uninstall", "--distpath", str(stage), "--workpath", str(stage.parent / "uninstall-build"), str(installer)], check=True)
    shutil.copy2(Path(__file__).with_name("README.md"), stage / "使用说明.md")
    files = [{"path": p.relative_to(stage).as_posix(), "bytes": p.stat().st_size, "sha256": sha(p)}
             for p in sorted(stage.rglob("*")) if p.is_file() and p.name != "bundle-manifest.json" and not system_runtime_file(p, stage)]
    manifest = {"version": __version__, "components": {"bridge": __version__, "video-freeze": "1.0.1", "kling-batch": "1.1.1", "beat-workshop": "0.5.0", "color-workshop": "1.0.1"}, "files": files}
    manifest["minimum_os"] = "Windows 10 x64"
    manifest["system_runtime"] = "Use the Universal C Runtime supplied by Windows 10/11; do not install a private system CRT."
    (stage / "bundle-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    archive = stage.parent / "payload.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for relative in [item["path"] for item in files] + ["bundle-manifest.json"]:
            z.write(stage / relative, relative)
    subprocess.run(common + ["--name", f"CodexHub-Setup-v{__version__}", "--add-data", str(archive) + ";.",
                            "--distpath", str(ROOT / "dist/suite"), "--workpath", str(stage.parent / "setup-build"), str(installer)], check=True)
    release = ROOT / f"dist/suite/CodexHub-Setup-v{__version__}.exe"
    manifest.update(installer=str(release), bytes=release.stat().st_size, sha256=sha(release))
    (release.parent / "release.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (release.parent / "SHA256SUMS.txt").write_text(f"{sha(release)}  {release.name}\n", encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("installer", "bytes", "sha256", "components")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
