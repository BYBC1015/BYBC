from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


def _string_set(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    return {str(item) for item in value}


def _delivery_map(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    deliveries: dict[str, dict[str, Any]] = {}
    for key, raw in value.items():
        if not isinstance(raw, dict):
            continue
        deliveries[str(key)] = {str(name): item for name, item in raw.items()}
    return deliveries


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def mention_delivery_key(project_slug: str, mention_id: int | str) -> str:
    return f"{str(project_slug).strip().lower()}:{mention_id}"


def task_delivery_key(project_slug: str, task_id: int | str) -> str:
    return f"{str(project_slug).strip().lower()}:{task_id}"


def _preserve_corrupt_state(path: Path, raw: bytes) -> Path | None:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for _attempt in range(10):
        backup = path.with_name(
            f"{path.name}.corrupt-{timestamp}-{uuid.uuid4().hex}.bak"
        )
        try:
            with backup.open("xb") as handle:
                handle.write(raw)
            return backup
        except FileExistsError:
            continue
        except OSError:
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                pass
            logger.exception(
                "Could not preserve corrupt bridge state file %s", path
            )
            return None
    logger.error("Could not allocate a unique corrupt-state backup for %s", path)
    return None


@dataclass
class BridgeState:
    initialized: bool = False
    seen_task_ids: set[str] = field(default_factory=set)
    prompted_task_ids: set[str] = field(default_factory=set)
    seen_mention_ids: set[str] = field(default_factory=set)
    prompted_mention_ids: set[str] = field(default_factory=set)
    acked_mention_ids: set[str] = field(default_factory=set)
    task_deliveries: dict[str, dict[str, Any]] = field(default_factory=dict)
    mention_deliveries: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "BridgeState":
        try:
            raw_bytes = path.read_bytes()
        except FileNotFoundError:
            return cls()
        try:
            raw = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            backup = _preserve_corrupt_state(path, raw_bytes)
            logger.warning(
                "Ignoring invalid bridge state file %s; backup=%s; error=%s",
                path,
                backup,
                exc,
            )
            return cls()
        if not raw.strip():
            return cls()
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("bridge state root must be a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            backup = _preserve_corrupt_state(path, raw_bytes)
            logger.warning(
                "Ignoring invalid bridge state file %s; backup=%s; error=%s",
                path,
                backup,
                exc,
            )
            return cls()
        return cls(
            initialized=bool(data.get("initialized", False)),
            seen_task_ids=_string_set(data.get("seen_task_ids")),
            prompted_task_ids=_string_set(data.get("prompted_task_ids")),
            seen_mention_ids=_string_set(data.get("seen_mention_ids")),
            prompted_mention_ids=_string_set(data.get("prompted_mention_ids")),
            acked_mention_ids=_string_set(data.get("acked_mention_ids")),
            task_deliveries=_delivery_map(data.get("task_deliveries")),
            mention_deliveries=_delivery_map(data.get("mention_deliveries")),
        )

    def get_task_delivery(
        self, project_slug: str, task_id: int | str
    ) -> dict[str, Any] | None:
        return self.task_deliveries.get(task_delivery_key(project_slug, task_id))

    def set_task_delivery(
        self,
        project_slug: str,
        task_id: int | str,
        stage: str,
        *,
        detail: str = "",
    ) -> dict[str, Any]:
        key = task_delivery_key(project_slug, task_id)
        current = dict(self.task_deliveries.get(key) or {})
        current.update(
            {
                "project_slug": str(project_slug),
                "task_id": int(task_id),
                "stage": str(stage),
                "updated_at": utc_now_text(),
            }
        )
        if detail:
            current["detail"] = str(detail)
        elif "detail" in current and stage == "submitted":
            current.pop("detail", None)
        self.task_deliveries[key] = current
        return current

    def remove_task_delivery(
        self, project_slug: str, task_id: int | str
    ) -> None:
        self.task_deliveries.pop(task_delivery_key(project_slug, task_id), None)

    def get_mention_delivery(
        self, project_slug: str, mention_id: int | str
    ) -> dict[str, Any] | None:
        return self.mention_deliveries.get(
            mention_delivery_key(project_slug, mention_id)
        )

    def set_mention_delivery(
        self,
        project_slug: str,
        mention_id: int | str,
        stage: str,
        *,
        delivery_id: str = "",
        detail: str = "",
    ) -> dict[str, Any]:
        key = mention_delivery_key(project_slug, mention_id)
        current = dict(self.mention_deliveries.get(key) or {})
        current.update(
            {
                "project_slug": str(project_slug),
                "mention_id": int(mention_id),
                "stage": str(stage),
                "updated_at": utc_now_text(),
            }
        )
        if delivery_id:
            current["delivery_id"] = str(delivery_id)
        if detail:
            current["detail"] = str(detail)
        elif "detail" in current and stage == "submitted":
            current.pop("detail", None)
        self.mention_deliveries[key] = current
        return current

    def remove_mention_delivery(
        self, project_slug: str, mention_id: int | str
    ) -> None:
        self.mention_deliveries.pop(
            mention_delivery_key(project_slug, mention_id), None
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "initialized": self.initialized,
            "seen_task_ids": sorted(self.seen_task_ids, key=int_or_text),
            "prompted_task_ids": sorted(self.prompted_task_ids, key=int_or_text),
            "seen_mention_ids": sorted(self.seen_mention_ids, key=int_or_text),
            "prompted_mention_ids": sorted(
                self.prompted_mention_ids, key=int_or_text
            ),
            "acked_mention_ids": sorted(self.acked_mention_ids, key=int_or_text),
            "task_deliveries": self.task_deliveries,
            "mention_deliveries": self.mention_deliveries,
        }
        temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        last_error: PermissionError | None = None
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                return
            except PermissionError as exc:
                last_error = exc
                time.sleep(0.02 * (attempt + 1))
        temporary.unlink(missing_ok=True)
        if last_error is not None:
            raise last_error


def int_or_text(value: str) -> tuple[int, int | str]:
    try:
        return (0, int(value))
    except ValueError:
        return (1, value)
