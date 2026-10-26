from __future__ import annotations

import re
from collections.abc import Mapping


QUESTION_MARK_RUN_RE = re.compile(r"\?{3,}")
MAX_PROJECT_MEMORY_TEXT_CHARS = 12_000
MAX_PROJECT_MEMORY_DOCUMENT_CHARS = 16_000


class SuspiciousTextEncodingError(ValueError):
    """Raised when text strongly resembles lossy Unicode-to-ASCII conversion."""


def looks_like_question_mark_encoding_loss(value: str) -> bool:
    """Detect high-confidence runs created when non-ASCII text becomes ``?``.

    The threshold is intentionally conservative so ordinary questions, templates,
    and short strings made of question marks remain valid Hub messages.
    """

    text = value or ""
    if len(text) < 40:
        return False

    runs = QUESTION_MARK_RUN_RE.findall(text)
    replacement_count = sum(len(run) for run in runs)
    visible_count = sum(not character.isspace() for character in text)
    if visible_count == 0:
        return False

    return (
        len(runs) >= 2
        and replacement_count >= 12
        and text.count("?") >= 16
        and replacement_count / visible_count >= 0.08
    )


def validate_device_entry_text(value: str) -> str:
    if looks_like_question_mark_encoding_loss(value):
        raise SuspiciousTextEncodingError(
            "Entry rejected because it looks like non-ASCII text was replaced "
            "with question marks. Submit UTF-8 JSON or read the reply from a "
            "UTF-8 body file; do not pipe Unicode through Windows PowerShell 5.1."
        )
    return value


def validate_project_memory_fields(values: Mapping[str, str]) -> dict[str, str]:
    """Validate canonical memory before a new immutable version is saved."""

    normalized = {
        str(name): str(value or "").strip() for name, value in values.items()
    }
    for name, text in normalized.items():
        if looks_like_question_mark_encoding_loss(text):
            raise SuspiciousTextEncodingError(
                f"Project memory field '{name}' was rejected because it looks like "
                "non-ASCII text was replaced with question marks. Submit UTF-8 "
                "JSON or an UTF-8 form body; do not pipe Unicode through Windows "
                "PowerShell 5.1."
            )

    total_characters = sum(len(text) for text in normalized.values())
    if total_characters > MAX_PROJECT_MEMORY_TEXT_CHARS:
        raise ValueError(
            "Project memory is too large for automatic AI injection "
            f"({total_characters} > {MAX_PROJECT_MEMORY_TEXT_CHARS} characters). "
            "Move verbose history into project files or handoffs and keep canonical "
            "memory concise."
        )
    return normalized


def validate_project_memory_document(value: str) -> str:
    """Validate rendered memory before the Bridge mirrors or injects it."""

    text = str(value or "").strip()
    if looks_like_question_mark_encoding_loss(text):
        raise SuspiciousTextEncodingError(
            "Project memory was not mirrored or injected because it looks like "
            "non-ASCII text was replaced with question marks."
        )
    if len(text) > MAX_PROJECT_MEMORY_DOCUMENT_CHARS:
        raise ValueError(
            "Project memory was not injected because it exceeds the runtime "
            f"limit ({len(text)} > {MAX_PROJECT_MEMORY_DOCUMENT_CHARS} characters)."
        )
    return text
