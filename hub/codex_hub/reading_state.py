from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import quote, urlencode

from .media_index import classify_attachment

ReaderType = Literal["user", "device"]
ResourceKind = Literal["markdown", "video"]
ContinueKind = Literal["image", "markdown", "video"]


@dataclass(frozen=True, slots=True)
class ReaderKey:
    reader_type: ReaderType
    reader_id: int

    def __post_init__(self) -> None:
        if self.reader_type not in {"user", "device"}:
            raise ValueError("reader_type must be user or device")
        if int(self.reader_id) <= 0:
            raise ValueError("reader_id must be positive")


@dataclass(frozen=True, slots=True)
class LongContentPolicy:
    min_characters: int = 2000
    min_lines: int = 40

    def __post_init__(self) -> None:
        if self.min_characters <= 0 or self.min_lines <= 0:
            raise ValueError("long-content thresholds must be positive")

    def is_long(self, markdown: str) -> bool:
        value = markdown or ""
        return (
            len(value) >= self.min_characters or value.count("\n") + 1 >= self.min_lines
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def initialize_reading_state_schema(connection: sqlite3.Connection) -> None:
    """Create the isolated reading-state tables.

    The reader is polymorphic because both browser users and device clients can
    resume content. Resource ownership is checked by ``ReadingStateStore``
    before every write.
    """

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS project_reading_states (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          reader_type TEXT NOT NULL CHECK (reader_type IN ('user', 'device')),
          reader_id INTEGER NOT NULL,
          last_image_attachment_id INTEGER REFERENCES attachments(id) ON DELETE SET NULL,
          last_markdown_entry_id INTEGER REFERENCES entries(id) ON DELETE SET NULL,
          last_video_attachment_id INTEGER REFERENCES attachments(id) ON DELETE SET NULL,
          last_read_entry_id INTEGER REFERENCES entries(id) ON DELETE SET NULL,
          continue_kind TEXT CHECK (
            continue_kind IS NULL OR continue_kind IN ('image', 'markdown', 'video')
          ),
          continue_resource_id INTEGER,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(project_id, reader_type, reader_id)
        );

        CREATE TABLE IF NOT EXISTS content_reading_states (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          reader_type TEXT NOT NULL CHECK (reader_type IN ('user', 'device')),
          reader_id INTEGER NOT NULL,
          resource_kind TEXT NOT NULL CHECK (resource_kind IN ('markdown', 'video')),
          resource_id INTEGER NOT NULL,
          anchor TEXT NOT NULL DEFAULT '',
          offset_value REAL NOT NULL DEFAULT 0,
          progress REAL,
          duration REAL,
          completed INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(project_id, reader_type, reader_id, resource_kind, resource_id)
        );

        CREATE INDEX IF NOT EXISTS idx_project_reading_reader_updated
          ON project_reading_states(reader_type, reader_id, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_content_reading_reader_updated
          ON content_reading_states(reader_type, reader_id, updated_at DESC);
        """
    )
    connection.commit()


def long_content_path(project_slug: str, entry_id: int) -> str:
    """Canonical path for the standalone long-Markdown reader."""

    if int(entry_id) <= 0:
        raise ValueError("entry_id must be positive")
    return f"/projects/{quote(project_slug, safe='')}/entries/{int(entry_id)}"


def describe_markdown_entry(
    project_slug: str,
    entry: dict[str, Any],
    policy: LongContentPolicy | None = None,
) -> dict[str, Any]:
    selected_policy = policy or LongContentPolicy()
    markdown = str(entry.get("body_markdown") or "")
    is_long = selected_policy.is_long(markdown)
    return {
        "entry_id": int(entry["id"]),
        "is_long_content": is_long,
        "long_content_url": (
            long_content_path(project_slug, int(entry["id"])) if is_long else None
        ),
    }


class ReadingStateStore:
    """Persistent, per-reader resume state and unread boundary calculations."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        long_content_policy: LongContentPolicy | None = None,
    ):
        self.connection = connection
        self.long_content_policy = long_content_policy or LongContentPolicy()

    def remember_image(
        self,
        project_id: int,
        reader: ReaderKey,
        attachment_id: int,
        *,
        mark_entry_read: bool = False,
    ) -> dict[str, Any]:
        attachment = self._attachment(project_id, attachment_id)
        if (
            classify_attachment(
                attachment["original_filename"], attachment["content_type"]
            )
            != "image"
        ):
            raise ValueError("attachment is not an image")
        self._ensure_state(project_id, reader)
        now = utc_now()
        self.connection.execute(
            """
            UPDATE project_reading_states
            SET last_image_attachment_id = ?,
                continue_kind = 'image',
                continue_resource_id = ?,
                updated_at = ?
            WHERE project_id = ? AND reader_type = ? AND reader_id = ?
            """,
            (
                attachment_id,
                attachment_id,
                now,
                project_id,
                reader.reader_type,
                reader.reader_id,
            ),
        )
        if mark_entry_read and attachment.get("entry_id"):
            self._advance_last_read(
                project_id, reader, int(attachment["entry_id"]), now
            )
        self.connection.commit()
        return self.snapshot(project_id, reader)

    def save_markdown_position(
        self,
        project_id: int,
        reader: ReaderKey,
        entry_id: int,
        *,
        anchor: str = "",
        offset: float = 0,
        progress: float | None = None,
        mark_entry_read: bool = False,
    ) -> dict[str, Any]:
        self._entry(project_id, entry_id)
        clean_offset = _non_negative(offset, "offset")
        clean_progress = _unit_interval(progress, "progress")
        clean_anchor = str(anchor or "").strip()[:512]
        self._ensure_state(project_id, reader)
        now = utc_now()
        self._upsert_resource_state(
            project_id,
            reader,
            "markdown",
            entry_id,
            anchor=clean_anchor,
            offset_value=clean_offset,
            progress=clean_progress,
            duration=None,
            completed=clean_progress == 1.0,
            now=now,
        )
        self.connection.execute(
            """
            UPDATE project_reading_states
            SET last_markdown_entry_id = ?,
                continue_kind = 'markdown',
                continue_resource_id = ?,
                updated_at = ?
            WHERE project_id = ? AND reader_type = ? AND reader_id = ?
            """,
            (
                entry_id,
                entry_id,
                now,
                project_id,
                reader.reader_type,
                reader.reader_id,
            ),
        )
        if mark_entry_read:
            self._advance_last_read(project_id, reader, entry_id, now)
        self.connection.commit()
        return self.get_markdown_position(project_id, reader, entry_id)

    def save_video_progress(
        self,
        project_id: int,
        reader: ReaderKey,
        attachment_id: int,
        *,
        position_seconds: float,
        duration_seconds: float | None = None,
        completed: bool | None = None,
        mark_entry_read: bool = False,
    ) -> dict[str, Any]:
        attachment = self._attachment(project_id, attachment_id)
        if (
            classify_attachment(
                attachment["original_filename"], attachment["content_type"]
            )
            != "video"
        ):
            raise ValueError("attachment is not a video")
        position = _non_negative(position_seconds, "position_seconds")
        existing = self._resource_state(project_id, reader, "video", attachment_id)
        if duration_seconds is None and existing:
            duration_seconds = existing.get("duration")
        duration = (
            _positive_number(duration_seconds, "duration_seconds")
            if duration_seconds is not None
            else None
        )
        if duration is not None:
            position = min(position, duration)
        inferred_completed = bool(duration and position >= max(0, duration - 1))
        is_completed = inferred_completed if completed is None else bool(completed)
        progress = 1.0 if is_completed else position / duration if duration else None

        self._ensure_state(project_id, reader)
        now = utc_now()
        self._upsert_resource_state(
            project_id,
            reader,
            "video",
            attachment_id,
            anchor="",
            offset_value=position,
            progress=progress,
            duration=duration,
            completed=is_completed,
            now=now,
        )
        self.connection.execute(
            """
            UPDATE project_reading_states
            SET last_video_attachment_id = ?,
                continue_kind = 'video',
                continue_resource_id = ?,
                updated_at = ?
            WHERE project_id = ? AND reader_type = ? AND reader_id = ?
            """,
            (
                attachment_id,
                attachment_id,
                now,
                project_id,
                reader.reader_type,
                reader.reader_id,
            ),
        )
        if mark_entry_read and attachment.get("entry_id"):
            self._advance_last_read(
                project_id, reader, int(attachment["entry_id"]), now
            )
        self.connection.commit()
        return self.get_video_progress(project_id, reader, attachment_id)

    def get_markdown_position(
        self, project_id: int, reader: ReaderKey, entry_id: int
    ) -> dict[str, Any] | None:
        state = self._resource_state(project_id, reader, "markdown", entry_id)
        return _public_resource_state(state) if state else None

    def get_video_progress(
        self, project_id: int, reader: ReaderKey, attachment_id: int
    ) -> dict[str, Any] | None:
        state = self._resource_state(project_id, reader, "video", attachment_id)
        return _public_resource_state(state) if state else None

    def mark_read_through(
        self,
        project_id: int,
        reader: ReaderKey,
        entry_id: int | None = None,
    ) -> dict[str, Any]:
        if entry_id is None:
            row = self.connection.execute(
                """
                SELECT MAX(id) AS entry_id
                FROM entries
                WHERE project_id = ? AND deleted_at IS NULL
                """,
                (project_id,),
            ).fetchone()
            entry_id = int(row["entry_id"]) if row and row["entry_id"] else None
        else:
            self._entry(project_id, entry_id)
        self._ensure_state(project_id, reader)
        if entry_id is not None:
            self._advance_last_read(project_id, reader, entry_id, utc_now())
        self.connection.commit()
        return self.unread_status(project_id, reader)

    def unread_status(self, project_id: int, reader: ReaderKey) -> dict[str, Any]:
        state = self._state(project_id, reader)
        last_read = (
            int(state["last_read_entry_id"])
            if state and state.get("last_read_entry_id")
            else 0
        )
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS unread_count,
                   MIN(id) AS first_unread_entry_id,
                   MAX(id) AS latest_entry_id
            FROM entries
            WHERE project_id = ? AND deleted_at IS NULL AND id > ?
            """,
            (project_id, last_read),
        ).fetchone()
        latest = self.connection.execute(
            """
            SELECT MAX(id) AS latest_entry_id
            FROM entries
            WHERE project_id = ? AND deleted_at IS NULL
            """,
            (project_id,),
        ).fetchone()
        unread_count = int(row["unread_count"] or 0)
        first_unread = row["first_unread_entry_id"]
        return {
            "last_read_entry_id": last_read or None,
            "latest_entry_id": latest["latest_entry_id"] if latest else None,
            "unread_count": unread_count,
            "has_unread": unread_count > 0,
            "first_unread_entry_id": first_unread,
            "divider_before_entry_id": first_unread,
        }

    def continue_reading(
        self, project_id: int, reader: ReaderKey
    ) -> dict[str, Any] | None:
        state = self._state(project_id, reader)
        if (
            not state
            or not state.get("continue_kind")
            or not state.get("continue_resource_id")
        ):
            return None
        project = self._project(project_id)
        kind = str(state["continue_kind"])
        resource_id = int(state["continue_resource_id"])
        slug = str(project["slug"])
        project_path = f"/projects/{quote(slug, safe='')}"

        if kind == "markdown":
            try:
                entry = self._entry(project_id, resource_id)
            except ValueError:
                return None
            description = describe_markdown_entry(slug, entry, self.long_content_policy)
            progress = self.get_markdown_position(project_id, reader, resource_id)
            return {
                "kind": kind,
                "resource_id": resource_id,
                "entry_id": resource_id,
                "attachment_id": None,
                "href": description["long_content_url"]
                or f"{project_path}#entry-{resource_id}",
                **description,
                "position": progress,
            }

        try:
            attachment = self._attachment(project_id, resource_id)
        except ValueError:
            return None
        expected = "image" if kind == "image" else "video"
        if (
            classify_attachment(
                attachment["original_filename"], attachment["content_type"]
            )
            != expected
        ):
            return None
        query = urlencode({"media": resource_id})
        return {
            "kind": kind,
            "resource_id": resource_id,
            "entry_id": attachment.get("entry_id"),
            "attachment_id": resource_id,
            "href": f"{project_path}?{query}#media-{resource_id}",
            "filename": attachment["original_filename"],
            "position": (
                self.get_video_progress(project_id, reader, resource_id)
                if kind == "video"
                else None
            ),
        }

    def snapshot(self, project_id: int, reader: ReaderKey) -> dict[str, Any]:
        self._project(project_id)
        state = self._state(project_id, reader)
        project_state = {
            "project_id": project_id,
            "reader_type": reader.reader_type,
            "reader_id": reader.reader_id,
            "last_image_attachment_id": None,
            "last_markdown_entry_id": None,
            "last_video_attachment_id": None,
            "updated_at": None,
        }
        if state:
            project_state.update(
                {
                    "last_image_attachment_id": state.get("last_image_attachment_id"),
                    "last_markdown_entry_id": state.get("last_markdown_entry_id"),
                    "last_video_attachment_id": state.get("last_video_attachment_id"),
                    "updated_at": state.get("updated_at"),
                }
            )
        project_state["unread"] = self.unread_status(project_id, reader)
        project_state["continue_reading"] = self.continue_reading(project_id, reader)
        return project_state

    def _ensure_state(self, project_id: int, reader: ReaderKey) -> None:
        self._project(project_id)
        self._reader(reader)
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO project_reading_states
              (project_id, reader_type, reader_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project_id, reader_type, reader_id) DO NOTHING
            """,
            (project_id, reader.reader_type, reader.reader_id, now, now),
        )

    def _advance_last_read(
        self, project_id: int, reader: ReaderKey, entry_id: int, now: str
    ) -> None:
        self.connection.execute(
            """
            UPDATE project_reading_states
            SET last_read_entry_id = CASE
                  WHEN last_read_entry_id IS NULL OR last_read_entry_id < ? THEN ?
                  ELSE last_read_entry_id
                END,
                updated_at = ?
            WHERE project_id = ? AND reader_type = ? AND reader_id = ?
            """,
            (
                entry_id,
                entry_id,
                now,
                project_id,
                reader.reader_type,
                reader.reader_id,
            ),
        )

    def _upsert_resource_state(
        self,
        project_id: int,
        reader: ReaderKey,
        resource_kind: ResourceKind,
        resource_id: int,
        *,
        anchor: str,
        offset_value: float,
        progress: float | None,
        duration: float | None,
        completed: bool,
        now: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO content_reading_states
              (project_id, reader_type, reader_id, resource_kind, resource_id,
               anchor, offset_value, progress, duration, completed,
               created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, reader_type, reader_id, resource_kind, resource_id)
            DO UPDATE SET
              anchor = excluded.anchor,
              offset_value = excluded.offset_value,
              progress = excluded.progress,
              duration = excluded.duration,
              completed = excluded.completed,
              updated_at = excluded.updated_at
            """,
            (
                project_id,
                reader.reader_type,
                reader.reader_id,
                resource_kind,
                resource_id,
                anchor,
                offset_value,
                progress,
                duration,
                int(completed),
                now,
                now,
            ),
        )

    def _state(self, project_id: int, reader: ReaderKey) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT *
            FROM project_reading_states
            WHERE project_id = ? AND reader_type = ? AND reader_id = ?
            """,
            (project_id, reader.reader_type, reader.reader_id),
        ).fetchone()
        return dict(row) if row else None

    def _resource_state(
        self,
        project_id: int,
        reader: ReaderKey,
        resource_kind: ResourceKind,
        resource_id: int,
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT *
            FROM content_reading_states
            WHERE project_id = ? AND reader_type = ? AND reader_id = ?
              AND resource_kind = ? AND resource_id = ?
            """,
            (
                project_id,
                reader.reader_type,
                reader.reader_id,
                resource_kind,
                resource_id,
            ),
        ).fetchone()
        return dict(row) if row else None

    def _project(self, project_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT id, slug FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if not row:
            raise ValueError("project does not exist")
        return dict(row)

    def _reader(self, reader: ReaderKey) -> dict[str, Any]:
        table = "users" if reader.reader_type == "user" else "devices"
        row = self.connection.execute(
            f"SELECT id FROM {table} WHERE id = ?", (reader.reader_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"{reader.reader_type} does not exist")
        return dict(row)

    def _entry(self, project_id: int, entry_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT * FROM entries
            WHERE id = ? AND project_id = ? AND deleted_at IS NULL
            """,
            (entry_id, project_id),
        ).fetchone()
        if not row:
            raise ValueError("entry does not belong to project")
        return dict(row)

    def _attachment(self, project_id: int, attachment_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM attachments WHERE id = ? AND project_id = ?",
            (attachment_id, project_id),
        ).fetchone()
        if not row:
            raise ValueError("attachment does not belong to project")
        return dict(row)


def _public_resource_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "resource_kind": state["resource_kind"],
        "resource_id": int(state["resource_id"]),
        "anchor": state["anchor"],
        "offset": float(state["offset_value"]),
        "progress": state["progress"],
        "duration": state["duration"],
        "completed": bool(state["completed"]),
        "updated_at": state["updated_at"],
    }


def _non_negative(value: float, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{label} must be a finite non-negative number")
    return parsed


def _positive_number(value: float, label: str) -> float:
    parsed = _non_negative(value, label)
    if parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed


def _unit_interval(value: float | None, label: str) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number") from exc
    if not math.isfinite(parsed) or not 0 <= parsed <= 1:
        raise ValueError(f"{label} must be between 0 and 1")
    return parsed
