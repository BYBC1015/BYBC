from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


WINDOWS_RELEASE_RE = re.compile(
    r"^(?:CodexBridge-v|CodexHub-Setup-v)(?P<version>\d+(?:\.\d+){1,3})\.exe$",
    re.IGNORECASE,
)
ANDROID_RELEASE_RE = re.compile(
    r"^CodexHub-v(?P<version>\d+(?:\.\d+){1,3})(?:-debug)?\.apk$",
    re.IGNORECASE,
)
BYBC_VIDEO_RELEASE_RE = re.compile(
    r"^BYBC_视频末帧延长工具_Setup_v(?P<version>\d+(?:\.\d+){1,3})\.exe$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Release:
    platform: str
    platform_label: str
    version: str
    path: Path
    byte_size: int
    sha256: str
    published_at: str

    def as_dict(self, download_url: str) -> dict[str, str | int]:
        return {
            "platform": self.platform,
            "platform_label": self.platform_label,
            "version": self.version,
            "filename": self.path.name,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
            "published_at": self.published_at,
            "download_url": download_url,
        }


def list_latest_releases(releases_dir: Path) -> list[Release]:
    candidates: dict[str, list[tuple[tuple[int, ...], Path, str]]] = {
        "windows": [],
        "android": [],
        "bybc-video": [],
    }
    if not releases_dir.exists():
        return []
    for path in releases_dir.iterdir():
        if not path.is_file():
            continue
        match = WINDOWS_RELEASE_RE.fullmatch(path.name)
        platform = "windows"
        if match is None:
            match = ANDROID_RELEASE_RE.fullmatch(path.name)
            platform = "android"
        if match is None:
            match = BYBC_VIDEO_RELEASE_RE.fullmatch(path.name)
            platform = "bybc-video"
        if match is None:
            continue
        version = match.group("version")
        candidates[platform].append((_version_key(version), path, version))

    releases: list[Release] = []
    for platform, platform_candidates in candidates.items():
        if not platform_candidates:
            continue
        _, path, version = max(platform_candidates, key=lambda item: (item[0], item[1].name.startswith("CodexHub-Setup-")))
        stat = path.stat()
        releases.append(
            Release(
                platform=platform,
                platform_label={
                    "windows": "Windows",
                    "android": "Android",
                    "bybc-video": "BYBC 视频工具",
                }[platform],
                version=version,
                path=path,
                byte_size=stat.st_size,
                sha256=_sha256(path),
                published_at=datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat(timespec="seconds"),
            )
        )
    return releases


def release_for_platform(
    releases: Iterable[Release], platform: str
) -> Release | None:
    normalized = platform.strip().casefold()
    return next(
        (release for release in releases if release.platform == normalized),
        None,
    )


def _version_key(value: str) -> tuple[int, ...]:
    parts = tuple(int(part) for part in value.split("."))
    return (*parts, *(0 for _ in range(4 - len(parts))))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
