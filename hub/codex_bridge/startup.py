from __future__ import annotations

import os
import re
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
        legacy = self.legacy_command_path
        if legacy == self.command_path or self.command_path.exists() or not legacy.is_file() or legacy.is_symlink():
            return
        # An adjacent MSI version must not claim another installation's startup
        # entry. Only migrate the simple command we generated for this same EXE
        # and these same arguments; leave customized/ambiguous scripts alone.
        try:
            if legacy.stat().st_size > 65536:
                return
            body = legacy.read_bytes()
            content = body.decode('utf-16' if body.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig')
            lines = [line.strip() for line in content.splitlines() if line.strip()]
            if lines and lines[0].lower() == '@echo off':
                lines.pop(0)
            if len(lines) != 1:
                return
            match = re.fullmatch(r'@?start\s+""\s+("[^"\r\n]+"(?:\s+.*)?)', lines[0], re.IGNORECASE)
            if not match:
                return
            command = _windows_arguments(match.group(1))
            if (not command or not Path(command[0]).is_absolute()
                    or os.path.normcase(str(Path(command[0]).resolve())) != os.path.normcase(str(exe_path.resolve()))
                    or command[1:] != list(args or [])):
                return
        except (OSError, UnicodeError, ValueError):
            return
        self.enable(exe_path, args=args)


def _windows_arguments(command: str) -> list[str]:
    import ctypes
    from ctypes import wintypes
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    count = ctypes.c_int()
    values = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not values:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [values[index] for index in range(count.value)]
    finally:
        kernel.LocalFree(values)


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
