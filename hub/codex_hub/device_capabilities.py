"""Persist only sanitized, device-authenticated runtime capabilities."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .model_catalog import normalize_device_capabilities


def save_device_capabilities(data_dir: Path, device_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    value = normalize_device_capabilities(payload)
    value["received_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    directory = data_dir / "device-capabilities"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{int(device_id)}.json"
    pending = directory / f"{int(device_id)}.{uuid.uuid4().hex}.tmp"
    try:
        pending.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(pending, target)
    finally:
        pending.unlink(missing_ok=True)
    return value


def load_device_capabilities(data_dir: Path, device_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    values: dict[int, dict[str, Any]] = {}
    for device_id in device_ids:
        try:
            path = data_dir / "device-capabilities" / f"{int(device_id)}.json"
            if path.stat().st_size > 256_000:
                continue
            value = json.loads(path.read_text(encoding="utf-8"))
            clean = normalize_device_capabilities(value)
            clean["received_at"] = str(value.get("received_at") or "")
            values[int(device_id)] = clean
        except (OSError, ValueError, TypeError):
            continue
    return values
