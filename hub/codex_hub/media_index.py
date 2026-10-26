from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

MediaCategory = Literal["image", "video", "document"]

IMAGE_EXTENSIONS = {
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".tif",
    ".tiff",
    ".webp",
}
VIDEO_EXTENSIONS = {
    ".avi",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".ogv",
    ".webm",
}
MEDIA_CATEGORIES = frozenset({"image", "video", "document"})
_DIMENSIONS_RE = re.compile(r"^\s*(\d+)\s*[xX\u00d7]\s*(\d+)\s*$")


@dataclass(frozen=True, slots=True)
class MediaFilters:
    """Filters supported by the project media index.

    ``message_id`` is the entry that owns an attachment. ``message_query``
    searches the owning entry body. Time bounds are inclusive and use the
    ISO-8601 strings already stored by Codex Hub.
    """

    category: MediaCategory | None = None
    message_id: int | None = None
    message_query: str = ""
    created_from: str | datetime | None = None
    created_to: str | datetime | None = None
    device_id: int | None = None
    generation_batch: str = ""


def classify_attachment(filename: str, content_type: str | None) -> MediaCategory:
    """Classify an attachment without treating PDF/Office files as previews."""

    media_type = (content_type or "").partition(";")[0].strip().casefold()
    suffix = Path(filename or "").suffix.casefold()
    if media_type.startswith("image/"):
        return "image"
    if media_type.startswith("video/"):
        return "video"
    if media_type and media_type not in {
        "application/octet-stream",
        "binary/octet-stream",
    }:
        return "document"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    return "document"


def parse_generation_metadata(
    raw_metadata: str | dict[str, Any] | None,
    *,
    attachment_id: int | None = None,
    filename: str = "",
) -> dict[str, Any]:
    """Normalize optional generation metadata from an entry.

    Producers have historically used root-level, ``generation`` and ``media``
    objects. Per-attachment overrides are also accepted so one message can
    contain outputs from more than one generation.
    """

    metadata = _metadata_object(raw_metadata)
    attachment_metadata = _find_attachment_metadata(
        metadata, attachment_id=attachment_id, filename=filename
    )
    sources = [
        attachment_metadata,
        _as_dict(metadata.get("media")),
        _as_dict(metadata.get("generation")),
        metadata,
    ]

    prompt = _first_value(sources, "prompt", "generation_prompt")
    model = _first_value(sources, "model", "model_name", "generator_model")
    parameters = _first_value(sources, "parameters", "params", "generation_parameters")
    batch = _first_value(
        sources,
        "generation_batch_id",
        "generation_batch",
        "batch_id",
        "batch",
    )
    width = _positive_int(_first_value(sources, "width", "image_width", "video_width"))
    height = _positive_int(
        _first_value(sources, "height", "image_height", "video_height")
    )
    if width is None or height is None:
        dimensions = _first_value(sources, "dimensions", "resolution", "size")
        parsed_width, parsed_height = _parse_dimensions(dimensions)
        width = width or parsed_width
        height = height or parsed_height

    result: dict[str, Any] = {}
    if prompt not in (None, ""):
        result["prompt"] = str(prompt)
    if width is not None and height is not None:
        result["width"] = width
        result["height"] = height
        result["dimensions"] = f"{width}\u00d7{height}"
    if model not in (None, ""):
        result["model"] = str(model)
    if parameters not in (None, "", {}, []):
        result["parameters"] = parameters
    if batch not in (None, ""):
        result["batch_id"] = str(batch)
    return result


def initialize_media_indexes(connection: sqlite3.Connection) -> None:
    """Install optional indexes used by media filters.

    Kept separate from the core schema so the main task can integrate it with
    one call and without changing the existing migration block.
    """

    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_attachments_project_created
          ON attachments(project_id, created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_attachments_project_device_created
          ON attachments(project_id, created_by_device_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_attachments_entry
          ON attachments(entry_id);
        """
    )
    connection.commit()


class MediaIndex:
    """Read-only categorized view over existing project attachments."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def list_items(
        self,
        project_id: int,
        filters: MediaFilters | None = None,
        *,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        filters = filters or MediaFilters()
        _validate_filters(filters)
        clean_limit = min(500, max(1, int(limit)))
        clean_offset = max(0, int(offset))

        clauses = ["attachments.project_id = ?"]
        params: list[Any] = [int(project_id)]
        if filters.message_id is not None:
            clauses.append("attachments.entry_id = ?")
            params.append(int(filters.message_id))
        if filters.device_id is not None:
            clauses.append(
                "COALESCE(attachments.created_by_device_id, "
                "entries.author_device_id) = ?"
            )
            params.append(int(filters.device_id))
        if filters.created_from is not None:
            clauses.append("attachments.created_at >= ?")
            params.append(_timestamp(filters.created_from))
        if filters.created_to is not None:
            clauses.append("attachments.created_at <= ?")
            params.append(_timestamp(filters.created_to))
        if filters.message_query.strip():
            clauses.append("entries.body_markdown LIKE ? ESCAPE '\\' COLLATE NOCASE")
            params.append(f"%{_escape_like(filters.message_query.strip())}%")

        rows = self.connection.execute(
            f"""
            SELECT attachments.*,
                   projects.slug AS project_slug,
                   entries.body_markdown AS message_markdown,
                   entries.metadata_json AS message_metadata_json,
                   entries.created_at AS message_created_at,
                   entries.source_label AS message_source_label,
                   entries.author_device_id AS message_device_id,
                   COALESCE(attachments.created_by_device_id,
                            entries.author_device_id) AS device_id,
                   devices.label AS device_label
            FROM attachments
            JOIN projects ON projects.id = attachments.project_id
            LEFT JOIN entries
              ON entries.id = attachments.entry_id
             AND entries.project_id = attachments.project_id
            LEFT JOIN devices
              ON devices.id = COALESCE(attachments.created_by_device_id,
                                       entries.author_device_id)
            WHERE {" AND ".join(clauses)}
              AND (entries.id IS NULL OR entries.deleted_at IS NULL)
            ORDER BY attachments.created_at DESC, attachments.id DESC
            """,
            tuple(params),
        ).fetchall()

        items: list[dict[str, Any]] = []
        wanted_batch = filters.generation_batch.strip()
        for row in rows:
            item = _media_item(dict(row))
            if filters.category and item["category"] != filters.category:
                continue
            if wanted_batch and item.get("generation_batch") != wanted_batch:
                continue
            items.append(item)
        return items[clean_offset : clean_offset + clean_limit]

    def category_counts(
        self, project_id: int, filters: MediaFilters | None = None
    ) -> dict[str, int]:
        """Count all matching items, ignoring only the category selection."""

        selected = filters or MediaFilters()
        without_category = MediaFilters(
            message_id=selected.message_id,
            message_query=selected.message_query,
            created_from=selected.created_from,
            created_to=selected.created_to,
            device_id=selected.device_id,
            generation_batch=selected.generation_batch,
        )
        counts = {"image": 0, "video": 0, "document": 0}
        # A project page is intentionally bounded. Use chunks so counts remain
        # correct if a project exceeds the public page-size cap.
        offset = 0
        while True:
            chunk = self.list_items(
                project_id, without_category, limit=500, offset=offset
            )
            for item in chunk:
                counts[item["category"]] += 1
            if len(chunk) < 500:
                break
            offset += len(chunk)
        return counts


def _media_item(row: dict[str, Any]) -> dict[str, Any]:
    category = classify_attachment(row["original_filename"], row["content_type"])
    generation = parse_generation_metadata(
        row.get("message_metadata_json"),
        attachment_id=int(row["id"]),
        filename=str(row["original_filename"]),
    )
    # Documents are deliberately list/download only. Even if an uploader put
    # generation-looking fields on a PDF or Office file, the UI should not
    # expose a preview/details treatment for it.
    if category == "document":
        generation = {}
    project_slug = str(row["project_slug"])
    download_url = (
        f"/projects/{quote(project_slug, safe='')}/attachments/{int(row['id'])}"
    )
    markdown = " ".join(str(row.get("message_markdown") or "").split())
    return {
        "id": int(row["id"]),
        "attachment_id": int(row["id"]),
        "project_id": int(row["project_id"]),
        "project_slug": project_slug,
        "message_id": row.get("entry_id"),
        "message_created_at": row.get("message_created_at"),
        "message_excerpt": markdown[:240],
        "filename": row["original_filename"],
        "content_type": row["content_type"],
        "byte_size": int(row["byte_size"]),
        "created_at": row["created_at"],
        "device_id": row.get("device_id"),
        "device_label": row.get("device_label")
        or row.get("message_source_label")
        or "",
        "category": category,
        "preview_kind": category if category in {"image", "video"} else None,
        "download_only": category == "document",
        "download_url": download_url,
        "preview_url": download_url if category in {"image", "video"} else None,
        "generation": generation,
        "generation_batch": generation.get("batch_id"),
        "prompt": generation.get("prompt"),
        "dimensions": generation.get("dimensions"),
        "model": generation.get("model"),
        "parameters": generation.get("parameters"),
    }


def _metadata_object(raw_metadata: str | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(raw_metadata, dict):
        return raw_metadata
    if not raw_metadata:
        return {}
    try:
        parsed = json.loads(raw_metadata)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _find_attachment_metadata(
    metadata: dict[str, Any], *, attachment_id: int | None, filename: str
) -> dict[str, Any]:
    attachments = metadata.get("attachments")
    if isinstance(attachments, dict):
        keys = [filename]
        if attachment_id is not None:
            keys.insert(0, str(attachment_id))
        for key in keys:
            value = attachments.get(key)
            if isinstance(value, dict):
                return value
    if isinstance(attachments, list):
        for value in attachments:
            if not isinstance(value, dict):
                continue
            value_id = value.get("attachment_id", value.get("id"))
            value_filename = value.get("filename", value.get("original_filename"))
            if attachment_id is not None and str(value_id) == str(attachment_id):
                return value
            if filename and str(value_filename) == filename:
                return value
    return {}


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_value(sources: list[dict[str, Any]], *keys: str) -> Any:
    for source in sources:
        for key in keys:
            if key in source and source[key] is not None:
                return source[key]
    return None


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_dimensions(value: Any) -> tuple[int | None, int | None]:
    if isinstance(value, dict):
        return _positive_int(value.get("width")), _positive_int(value.get("height"))
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return _positive_int(value[0]), _positive_int(value[1])
    match = _DIMENSIONS_RE.match(str(value or ""))
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _validate_filters(filters: MediaFilters) -> None:
    if filters.category is not None and filters.category not in MEDIA_CATEGORIES:
        raise ValueError("category must be image, video, or document")
    if filters.message_id is not None and int(filters.message_id) <= 0:
        raise ValueError("message_id must be positive")
    if filters.device_id is not None and int(filters.device_id) <= 0:
        raise ValueError("device_id must be positive")
    if (
        filters.created_from is not None
        and filters.created_to is not None
        and _timestamp(filters.created_from) > _timestamp(filters.created_to)
    ):
        raise ValueError("created_from must not be after created_to")


def _timestamp(value: str | datetime) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        clean = str(value).strip()
        if not clean:
            raise ValueError("timestamp filter cannot be empty")
        try:
            parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("timestamp filter must be ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
