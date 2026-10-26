from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes


ERROR_ALREADY_EXISTS = 183
DEFAULT_MUTEX_NAME = r"Local\CodexBridge.MultiAI.Singleton.v1"


class SingleInstance:
    def __init__(self, name: str = DEFAULT_MUTEX_NAME):
        self.name = name
        self.handle: int | None = None

    def acquire(self) -> bool:
        if not sys.platform.startswith("win"):
            return True
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self.handle = int(handle)
        return True

    def close(self) -> None:
        if self.handle and kernel32 is not None:
            kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self) -> "SingleInstance":
        if not self.acquire():
            raise RuntimeError("CodexBridge is already running")
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


def notify_already_running() -> None:
    if not sys.platform.startswith("win"):
        return
    user32.MessageBoxW(
        None,
        "CodexBridge 已在运行，无需重复启动。",
        "Codex Bridge",
        0x00000040,
    )


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if sys.platform.startswith("win") else None
user32 = ctypes.WinDLL("user32", use_last_error=True) if sys.platform.startswith("win") else None

if kernel32 is not None:
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
