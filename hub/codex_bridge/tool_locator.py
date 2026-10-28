from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable

from .subprocess_utils import hidden_subprocess_kwargs


def first_existing_path(candidates: Iterable[Path]) -> Path | None:
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def find_default_codex_path(candidates: Iterable[Path] | None = None) -> Path | None:
    if candidates is not None:
        return first_existing_path(candidates)

    running = find_running_codex_path()
    if running:
        return running

    found: list[Path] = []
    windows_apps = Path(r"C:\Program Files\WindowsApps")
    if windows_apps.exists():
        found.extend(windows_apps.glob(r"OpenAI.Codex_*\app\ChatGPT.exe"))
        found.extend(windows_apps.glob(r"OpenAI.Codex_*\app\Codex.exe"))

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        found.append(Path(local_app_data) / "Microsoft" / "WindowsApps" / "Codex.exe")

    return first_existing_path(found)


def find_default_codex_cli_path(
    candidates: Iterable[Path] | None = None,
) -> Path | None:
    if candidates is not None:
        return _first_cli_file(candidates)

    found: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        found.extend(
            [
                Path(appdata) / "npm" / "codex.cmd",
                Path(appdata) / "npm" / "codex",
            ]
        )

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        found.append(Path(local_app_data) / "Microsoft" / "WindowsApps" / "codex.cmd")

    return _first_cli_file(found)


def _first_cli_file(candidates: Iterable[Path]) -> Path | None:
    for candidate in candidates:
        try:
            path = Path(candidate).expanduser()
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def resolve_codex_cli_path(configured: str | Path | None = None) -> Path | None:
    """Keep a valid configured CLI; recover missing paths from installed CLIs.

    This performs filesystem discovery only. A CLI that exists but fails to
    execute is not retried with another installation.
    """
    if configured:
        path = Path(configured).expanduser()
        explicit = _first_cli_file([path])
        if explicit:
            return explicit
        # Keep desktop CLI/model compatibility after an upgrade replaces the
        # hash directory. Never select the similarly named desktop GUI binary.
        if (path.name.lower() == "codex.exe" and len(path.parents) >= 4
                and [part.name.lower() for part in path.parents[1:4]] == ["bin", "codex", "openai"]):
            bundled: list[tuple[int, str, Path]] = []
            try:
                for candidate in path.parents[1].glob("*/codex.exe"):
                    try:
                        if candidate.is_file():
                            bundled.append((candidate.stat().st_mtime_ns, str(candidate), candidate))
                    except OSError:
                        continue
            except OSError:
                pass
            # A separate npm CLI may support a different account/model set.
            # If this desktop family is gone, report it instead of switching.
            return max(bundled)[2] if bundled else None
    return find_default_codex_cli_path()


def find_running_codex_path() -> Path | None:
    if not sys.platform.startswith("win"):
        return None
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                (
                    "Get-CimInstance Win32_Process "
                    "| Where-Object { $_.Name -in @('ChatGPT.exe','Codex.exe') } "
                    "| Select-Object -ExpandProperty ExecutablePath"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            **hidden_subprocess_kwargs(),
        )
    except Exception:
        return None
    return parse_codex_exe_path(completed.stdout)


def parse_codex_exe_path(output: str) -> Path | None:
    for line in output.splitlines():
        text = line.strip()
        if not text:
            continue
        lowered = text.lower()
        if (
            lowered.endswith((r"\app\chatgpt.exe", r"\app\codex.exe"))
            and "\\resources\\" not in lowered
            and "\\windowsapps\\openai.codex_" in lowered
        ):
            return Path(text)
    return None
