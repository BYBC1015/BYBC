"""Bounded operational evidence for weekly maintenance, without raw log content."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
from typing import Iterable
from urllib.request import urlopen

SERVICES = ("codex-hub", "codex-hub-notifications")
ERROR_NAMES = ("TimeoutError", "ConnectionError", "OperationalError", "IntegrityError",
               "PermissionError", "FileNotFoundError", "ValueError", "HTTPStatusError")
ACCESS = re.compile(r'"(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) [^"\r\n]{1,4096} HTTP/\d(?:\.\d)?" (\d{3})\b')


def summarize_journal(lines: Iterable[str], *, limit: int = 2000) -> dict:
    """Only fixed labels and numbers leave this boundary, never arbitrary messages."""
    statuses: Counter = Counter()
    errors: Counter = Counter()
    parsed = invalid = examined = 0
    for line in lines:
        if examined >= limit:
            break
        examined += 1
        if len(line) > 65536:
            invalid += 1
            continue
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            invalid += 1
            continue
        if not isinstance(record, dict):
            invalid += 1
            continue
        message = record.get("MESSAGE", "")
        if not isinstance(message, str):
            invalid += 1
            continue
        parsed += 1
        message = message[:8192]
        match = ACCESS.search(message)
        if match:
            statuses[str(int(match.group(1)) // 100) + "xx"] += 1
        for label in ERROR_NAMES:
            if re.search(r"\b" + label + r"\b", message):
                errors[label] += 1
        if "Traceback (most recent call last)" in message:
            errors["tracebacks"] += 1
    return {"sampled_records": parsed, "unreadable_records": invalid,
            "sample_limit": limit, "http_status_classes": dict(statuses),
            "error_categories": dict(errors),
            "scope": "bounded recent sample; absence is not proof of zero failures"}


def collect_diagnostics(since_hours: int = 168) -> dict:
    hours = min(720, max(1, int(since_hours)))
    result = {"collected_at": datetime.now(timezone.utc).isoformat(),
              "requested_hours": hours, "services": {}}
    for service in SERVICES:
        evidence = {"state": "unavailable"}
        try:
            state = subprocess.run(["systemctl", "is-active", service], capture_output=True,
                                   text=True, timeout=5, check=False).stdout.strip()
            evidence["state"] = state if state in {"active", "inactive", "failed", "activating"} else "unknown"
            journal = subprocess.run(["journalctl", "-u", service, "--since", f"{hours} hours ago",
                                      "-n", "1000", "-o", "json", "--no-pager", "--quiet"],
                                     capture_output=True, text=True, timeout=15, check=False)
            evidence["logs_available"] = journal.returncode == 0
            evidence["journal"] = summarize_journal(journal.stdout.splitlines(), limit=1000)
        except (OSError, subprocess.TimeoutExpired):
            evidence["logs_available"] = False
        result["services"][service] = evidence
    result["health"] = {"host": "127.0.0.1", "port": 8797, "ok": False}
    try:
        with urlopen("http://127.0.0.1:8797/healthz", timeout=5) as response:
            payload = json.loads(response.read(4096))
            result["health"]["ok"] = isinstance(payload, dict) and payload.get("ok") is True
    except (OSError, ValueError):
        pass
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = collect_diagnostics()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(args.output)
    print("Maintenance diagnostic summary collected; raw logs excluded.")


if __name__ == "__main__":
    main()
