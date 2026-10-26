from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .subprocess_utils import hidden_subprocess_kwargs


class CodexLauncher:
    def __init__(self, codex_exe_path: Path | str | None = None):
        self.codex_exe_path = Path(codex_exe_path) if codex_exe_path else None

    def open(self, url: str) -> None:
        if sys.platform.startswith("win"):
            try:
                os.startfile(url)  # type: ignore[attr-defined]
                return
            except OSError:
                if self.codex_exe_path and self.codex_exe_path.exists():
                    subprocess.Popen(
                        [str(self.codex_exe_path), url],
                        **hidden_subprocess_kwargs(),
                    )
                    return
                raise
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", url], **hidden_subprocess_kwargs())
            return
        subprocess.Popen(["xdg-open", url], **hidden_subprocess_kwargs())
