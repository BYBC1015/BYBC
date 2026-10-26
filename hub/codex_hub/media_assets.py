from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .media_index import parse_generation_metadata
from .reading import is_video_attachment, video_content_type

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover - production dependency, graceful dev fallback
    Image = None
    ImageOps = None


IMAGE_CONTENT_TYPES = frozenset(
    {"image/gif", "image/jpeg", "image/png", "image/webp"}
)
IMAGE_SUFFIXES = frozenset({".gif", ".jpeg", ".jpg", ".png", ".webp"})
VIDEO_SUFFIXES = frozenset(
    {".mp4", ".m4v", ".mov", ".webm", ".ogv", ".ogg", ".avi", ".mkv"}
)
THUMBNAIL_SIZES = {"thumb": 360, "preview": 1440}


def media_type_for(attachment: dict[str, Any]) -> str:
    content_type = str(attachment.get("content_type") or "").lower()
    suffix = Path(str(attachment.get("original_filename") or "")).suffix.lower()
    if content_type in IMAGE_CONTENT_TYPES or suffix in IMAGE_SUFFIXES:
        return "image"
    if is_video_attachment(attachment):
        return "video"
    return "document"


def human_file_size(value: int | str | None) -> str:
    try:
        size = max(0, int(value or 0))
    except (TypeError, ValueError):
        size = 0
    units = ("B", "KB", "MB", "GB", "TB")
    number = float(size)
    unit = units[0]
    for unit in units:
        if number < 1024 or unit == units[-1]:
            break
        number /= 1024
    if unit == "B":
        return f"{int(number)} {unit}"
    return f"{number:.1f} {unit}"


def safe_metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _first(metadata: dict[str, Any], *keys: str) -> Any:
    sources = [metadata]
    for nested_key in ("generation", "image_generation", "media", "output"):
        nested = metadata.get(nested_key)
        if isinstance(nested, dict):
            sources.append(nested)
    for source in sources:
        for key in keys:
            value = source.get(key)
            if value not in (None, "", [], {}):
                return value
    return ""


def _stringify_params(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def image_dimensions(path: Path) -> tuple[int | None, int | None]:
    if Image is None or not path.is_file():
        return None, None
    try:
        with Image.open(path) as image:
            width, height = image.size
        if width > 0 and height > 0:
            return int(width), int(height)
    except (OSError, ValueError):
        pass
    return None, None


def decorate_attachment(
    attachment: dict[str, Any],
    *,
    entry: dict[str, Any] | None = None,
    files_root: Path | None = None,
) -> dict[str, Any]:
    decorated = dict(attachment)
    entry = entry or {}
    metadata = safe_metadata(entry.get("metadata_json"))
    generation = parse_generation_metadata(
        metadata,
        attachment_id=int(decorated.get("id") or 0) or None,
        filename=str(decorated.get("original_filename") or ""),
    )
    kind = media_type_for(decorated)
    width: int | None = None
    height: int | None = None
    if kind == "image" and files_root:
        candidate = (files_root / str(decorated.get("stored_relpath") or "")).resolve()
        root = files_root.resolve()
        if root in candidate.parents:
            width, height = image_dimensions(candidate)
    width = width or generation.get("width")
    height = height or generation.get("height")

    decorated.update(
        {
            "media_type": kind,
            "content_type": (
                video_content_type(decorated)
                if kind == "video"
                else decorated.get("content_type")
            )
            or "application/octet-stream",
            "is_inline_image": kind == "image",
            "is_inline_video": kind == "video",
            "byte_size_display": human_file_size(decorated.get("byte_size")),
            "source_label": str(entry.get("source_label") or "未知来源"),
            "entry_created_at": str(entry.get("created_at") or decorated.get("created_at") or ""),
            "width": width,
            "height": height,
            "aspect_ratio_css": f"{width} / {height}" if width and height else "4 / 3",
            "generation_prompt": str(generation.get("prompt") or ""),
            "generation_model": str(generation.get("model") or ""),
            "generation_batch": str(generation.get("batch_id") or ""),
            "generation_params": _stringify_params(generation.get("parameters")),
        }
    )
    return decorated


def thumbnail_path(
    files_root: Path,
    attachment: dict[str, Any],
    variant: str,
) -> Path | None:
    max_dimension = THUMBNAIL_SIZES.get(variant)
    if not max_dimension or Image is None or ImageOps is None:
        return None
    if media_type_for(attachment) != "image":
        return None

    root = files_root.resolve()
    original = (root / str(attachment.get("stored_relpath") or "")).resolve()
    if root not in original.parents or not original.is_file():
        return None
    sha = str(attachment.get("sha256") or "")
    safe_sha = sha if len(sha) >= 12 and sha.isalnum() else str(attachment.get("id") or "file")
    target = root / ".derived" / safe_sha[:2] / f"{safe_sha}-{variant}.webp"
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp.webp")
    try:
        with Image.open(original) as source:
            source.seek(0)
            image = ImageOps.exif_transpose(source)
            image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA" if "transparency" in image.info else "RGB")
            image.save(temporary, format="WEBP", quality=84, method=5)
        temporary.replace(target)
        return target
    except (OSError, ValueError):
        temporary.unlink(missing_ok=True)
        return None
