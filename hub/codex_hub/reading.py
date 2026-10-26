"""Presentation helpers for safe, on-demand attachment reading.

This module deliberately has no dependency on the web application.  Route and
template code can opt into it without coupling attachment classification to a
particular page.
"""

from __future__ import annotations

from pathlib import PurePath
from typing import Any, Mapping


VIDEO_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "video/mp4",
        "video/webm",
        "video/ogg",
        "video/quicktime",
        "video/x-m4v",
    }
)

VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {".mp4", ".webm", ".ogv", ".ogg", ".mov", ".m4v"}
)

GENERIC_CONTENT_TYPES: frozenset[str] = frozenset(
    {"", "application/octet-stream", "binary/octet-stream"}
)

# These formats remain downloads even if an uploader supplies a misleading
# video MIME type.  The scope intentionally includes active web documents as
# well as the explicitly out-of-scope PDF and Office families.
ORDINARY_ATTACHMENT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".doc",
        ".docm",
        ".docx",
        ".htm",
        ".html",
        ".odp",
        ".ods",
        ".odt",
        ".pdf",
        ".ppt",
        ".pptm",
        ".pptx",
        ".rtf",
        ".svg",
        ".xls",
        ".xlsm",
        ".xlsx",
        ".xml",
    }
)

_CONTENT_TYPE_BY_EXTENSION = {
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".mov": "video/quicktime",
    ".ogg": "video/ogg",
    ".ogv": "video/ogg",
    ".webm": "video/webm",
}


def normalized_content_type(value: object) -> str:
    """Return the MIME type without parameters, normalized for comparison."""

    return str(value or "").split(";", 1)[0].strip().lower()


def attachment_extension(filename: object) -> str:
    """Return a lowercase final suffix without trusting any path component."""

    return PurePath(str(filename or "").replace("\\", "/")).suffix.lower()


def is_video_attachment(attachment: Mapping[str, Any]) -> bool:
    """Classify browser-readable video conservatively.

    A recognized MIME type wins.  A recognized extension is accepted only
    when the uploader supplied no useful MIME type.  This prevents an HTML,
    SVG, PDF, or Office attachment with a misleading suffix from becoming an
    inline media surface.
    """

    extension = attachment_extension(attachment.get("original_filename"))
    if extension in ORDINARY_ATTACHMENT_EXTENSIONS:
        return False
    content_type = normalized_content_type(attachment.get("content_type"))
    if content_type in VIDEO_CONTENT_TYPES:
        return True
    if content_type not in GENERIC_CONTENT_TYPES:
        return False
    return extension in VIDEO_EXTENSIONS


def video_content_type(attachment: Mapping[str, Any]) -> str | None:
    """Return a supported source MIME type, inferring it for generic uploads."""

    if not is_video_attachment(attachment):
        return None
    content_type = normalized_content_type(attachment.get("content_type"))
    if content_type in VIDEO_CONTENT_TYPES:
        return content_type
    return _CONTENT_TYPE_BY_EXTENSION.get(
        attachment_extension(attachment.get("original_filename"))
    )


def format_byte_size(value: object) -> str:
    """Format an attachment size for its pre-load information card."""

    try:
        size = max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        size = 0
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(size)
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    if unit == "B":
        return f"{int(amount)} {unit}"
    return f"{amount:.1f} {unit}"


def video_attachment_view(
    attachment: Mapping[str, Any],
    *,
    source_url: str,
    poster_url: str | None = None,
) -> dict[str, Any] | None:
    """Build template-neutral metadata for the lazy video reader.

    ``None`` means the attachment must remain an ordinary download.  In
    particular, PDF and Office documents are intentionally never previewed.
    """

    if not is_video_attachment(attachment):
        return None
    filename = str(attachment.get("original_filename") or "video")
    try:
        byte_size = max(0, int(attachment.get("byte_size") or 0))
    except (TypeError, ValueError, OverflowError):
        byte_size = 0
    return {
        "id": str(attachment.get("id") or source_url),
        "filename": filename,
        "content_type": video_content_type(attachment),
        "byte_size": byte_size,
        "size_label": format_byte_size(byte_size),
        "source_url": str(source_url),
        "poster_url": str(poster_url) if poster_url else None,
    }
