from __future__ import annotations

import os
import sys
from pathlib import Path


class StartupManager:
    def __init__(
        self,
        startup_dir: Path | None = None,
        shortcut_name: str = "CodexBridge.vbs",
    ):
        self.startup_dir = startup_dir or windows_startup_dir()
        self.shortcut_name = shortcut_name

    @property
    def command_path(self) -> Path:
        return self.startup_dir / self.shortcut_name

    @property
    def legacy_command_path(self) -> Path:
        return self.startup_dir / f"{self.command_path.stem}.cmd"

    def is_enabled(self) -> bool:
        return self.command_path.exists() or self.legacy_command_path.exists()

    def set_enabled(
        self,
        enabled: bool,
        exe_path: Path | None = None,
        args: list[str] | None = None,
    ) -> None:
        if enabled:
            self.enable(exe_path or Path(sys.executable), args=args)
        else:
            self.disable()

    def enable(self, exe_path: Path, args: list[str] | None = None) -> None:
        self.startup_dir.mkdir(parents=True, exist_ok=True)
        suffix = ""
        if args:
            suffix = " " + " ".join(quote_arg(arg) for arg in args)
        if self.command_path.suffix.lower() == ".vbs":
            command_line = f'"{exe_path}"{suffix}'
            escaped = command_line.replace('"', '""')
            command = (
                'Set shell = CreateObject("WScript.Shell")\r\n'
                f'shell.Run "{escaped}", 0, False\r\n'
            )
        else:
            command = (
                "@echo off\r\n"
                f'start "" "{exe_path}"{suffix}\r\n'
            )
        # WScript interprets BOM-less scripts as the system ANSI codepage.
        # UTF-16 preserves custom Chinese installation paths on every Windows locale.
        encoding = "utf-16" if self.command_path.suffix.lower() == ".vbs" else "utf-8"
        self.command_path.write_text(command, encoding=encoding)
        if self.legacy_command_path != self.command_path and self.legacy_command_path.exists():
            self.legacy_command_path.unlink()

    def disable(self) -> None:
        if self.command_path.exists():
            self.command_path.unlink()
        if self.legacy_command_path != self.command_path and self.legacy_command_path.exists():
            self.legacy_command_path.unlink()

    def remove_if_owned(self, exe_path: Path) -> None:
        for path in {self.command_path, self.legacy_command_path}:
            if not path.is_file():
                continue
            data = path.read_bytes()
            encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
            content = data.decode(encoding).replace('""', '"').casefold()
            if f'"{exe_path}"'.casefold() in content:
                path.unlink()

    def migrate_legacy(self, exe_path: Path, args: list[str] | None = None) -> None:
        if self.legacy_command_path.exists() and self.legacy_command_path != self.command_path:
            self.enable(exe_path, args=args)


def windows_startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return Path.home() / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup"
    return (
        Path(appdata)
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
    )


def quote_arg(value: str) -> str:
    if not value or any(ch.isspace() for ch in value):
        return '"' + value.replace('"', '\\"') + '"'
    return value
