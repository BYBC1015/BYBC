from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

from codex_bridge.cli import main


def write_launcher_log(message: str) -> None:
    try:
        appdata = Path(os.environ.get("APPDATA") or Path.home())
        log_path = appdata / "CodexBridge" / "launcher.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().isoformat(timespec="seconds")
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")
    except Exception:
        return


if __name__ == "__main__":
    write_launcher_log(f"start argv={sys.argv[1:]}")
    try:
        exit_code = main()
        write_launcher_log(f"exit code={exit_code}")
        raise SystemExit(exit_code)
    except SystemExit as exc:
        write_launcher_log(f"system exit code={exc.code}")
        raise
    except BaseException:
        write_launcher_log("unhandled exception:\n" + traceback.format_exc())
        raise
