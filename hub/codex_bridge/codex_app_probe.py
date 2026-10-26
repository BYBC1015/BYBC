from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .subprocess_utils import hidden_subprocess_kwargs


class WindowsCodexAppProbe:
    """Detect the main Codex desktop process for a failed Hub delivery."""

    marker = "CODEX_DESKTOP_RUNNING"

    def is_running(self, configured_path: Path | None = None) -> bool:
        if not sys.platform.startswith("win"):
            return False

        configured = _powershell_literal(str(configured_path or ""))
        command = (
            f"$configured = '{configured}'; "
            "$found = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
            "Where-Object { "
            "$path = [string]$_.ExecutablePath; "
            "$cmd = [string]$_.CommandLine; "
            "$isMain = $cmd -notmatch '(?:^|\\s)--type='; "
            "$isPackage = $path -match "
            "'(?i)\\\\WindowsApps\\\\OpenAI\\.Codex_[^\\\\]+\\\\app\\\\"
            "(?:ChatGPT|Codex)\\.exe$'; "
            "$isConfigured = $configured -and "
            "$path.Equals($configured, [System.StringComparison]::OrdinalIgnoreCase); "
            "$isMain -and ($isPackage -or $isConfigured) "
            "} | Select-Object -First 1; "
            f"if ($found) {{ Write-Output '{self.marker}' }}"
        )
        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
                **hidden_subprocess_kwargs(),
            )
        except Exception:
            return False
        return completed.returncode == 0 and self.marker in completed.stdout


def _powershell_literal(value: str) -> str:
    return str(value or "").replace("'", "''")
