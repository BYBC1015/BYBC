from __future__ import annotations

import subprocess
import sys
from typing import Any


def hidden_subprocess_kwargs() -> dict[str, Any]:
    if not sys.platform.startswith("win"):
        return {}
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    kwargs: dict[str, Any] = {"startupinfo": startupinfo}
    if flag:
        kwargs["creationflags"] = flag
    return kwargs
