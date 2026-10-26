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
        return first_existing_path(candidates)

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

    return first_existing_path(found)


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
