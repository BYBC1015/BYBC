from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path


class PersistentLog:
    def __init__(self, path: Path, max_bytes: int = 2 * 1024 * 1024):
        self.path = Path(path)
        self.max_bytes = max(1024, int(max_bytes))
        self._lock = threading.Lock()

    def write(self, message: str) -> None:
        line = f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_if_needed(len(line.encode("utf-8")))
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def tail(self, limit: int = 200) -> list[str]:
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return []
        return lines[-max(1, int(limit)) :]

    def clear(self) -> None:
        with self._lock:
            for target in (self.path, self.path.with_suffix(self.path.suffix + ".1")):
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        try:
            current_size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if current_size + incoming_bytes <= self.max_bytes:
            return
        backup = self.path.with_suffix(self.path.suffix + ".1")
        if backup.exists():
            backup.unlink()
        self.path.replace(backup)
