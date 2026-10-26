from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Any


PROJECT_MEMORY_FIELDS = (
    "goal",
    "non_goals",
    "constraints_text",
    "current_phase",
    "acceptance",
    "roles",
    "architecture",
    "decisions",
    "known_issues",
    "risks",
    "user_preferences",
    "key_files",
    "next_steps",
    "handoff_summary",
)
PROJECT_MEMORY_SECTIONS = (
    ("Goal", "goal"),
    ("Non-goals", "non_goals"),
    ("Constraints", "constraints_text"),
    ("Current Phase", "current_phase"),
    ("Acceptance", "acceptance"),
    ("Roles", "roles"),
    ("Architecture", "architecture"),
    ("Decisions", "decisions"),
    ("Known Issues", "known_issues"),
    ("Risks", "risks"),
    ("User Preferences", "user_preferences"),
    ("Key Files", "key_files"),
    ("Next Steps", "next_steps"),
    ("Handoff Summary", "handoff_summary"),
)
RUNTIME_PROJECT_MEMORY_FIELDS = {
    "goal",
    "non_goals",
    "constraints_text",
    "current_phase",
    "acceptance",
    "decisions",
    "known_issues",
    "risks",
    "user_preferences",
    "next_steps",
}
HANDOFF_STATUS_VALUES = {"draft", "ready", "accepted", "superseded"}
MANIFEST_SCHEMA_VERSION = 1
DEFAULT_LARGE_FILE_THRESHOLD = 100 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def normalize_project_manifest(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        raw: dict[str, Any] = {}
    elif isinstance(value, str):
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("manifest must be a JSON object")
        raw = parsed
    elif isinstance(value, dict):
        raw = value
    else:
        raise ValueError("manifest must be an object")

    raw_files = raw.get("files") or []
    if not isinstance(raw_files, list):
        raise ValueError("manifest files must be a list")
    if len(raw_files) > 20_000:
        raise ValueError("manifest contains too many files")

    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_files:
        if not isinstance(item, dict):
            raise ValueError("manifest file entries must be objects")
        path = _normalize_relative_path(item.get("path"))
        if path in seen:
            raise ValueError(f"manifest contains duplicate path: {path}")
        seen.add(path)
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid size for {path}") from exc
        if size < 0:
            raise ValueError(f"invalid size for {path}")
        sha256 = str(item.get("sha256") or "").strip().lower()
        if sha256 and not SHA256_RE.fullmatch(sha256):
            raise ValueError(f"invalid sha256 for {path}")
        files.append(
            {
                "path": path,
                "size": size,
                "sha256": sha256,
                "kind": str(item.get("kind") or "file").strip()[:40] or "file",
                "required": bool(item.get("required", False)),
            }
        )

    files.sort(key=lambda item: item["path"].casefold())
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": str(raw.get("generated_at") or ""),
        "git_commit": str(raw.get("git_commit") or "").strip()[:160],
        "files": files,
    }


def compare_project_manifests(
    source: Any,
    target: Any,
    *,
    large_file_threshold: int = DEFAULT_LARGE_FILE_THRESHOLD,
) -> dict[str, Any]:
    source_manifest = normalize_project_manifest(source)
    target_manifest = normalize_project_manifest(target)
    source_files = {item["path"]: item for item in source_manifest["files"]}
    target_files = {item["path"]: item for item in target_manifest["files"]}

    missing_on_target = [
        source_files[path]
        for path in sorted(source_files.keys() - target_files.keys(), key=str.casefold)
    ]
    missing_on_source = [
        target_files[path]
        for path in sorted(target_files.keys() - source_files.keys(), key=str.casefold)
    ]
    changed: list[dict[str, Any]] = []
    identical = 0
    for path in sorted(source_files.keys() & target_files.keys(), key=str.casefold):
        source_item = source_files[path]
        target_item = target_files[path]
        source_identity = (source_item["sha256"], source_item["size"])
        target_identity = (target_item["sha256"], target_item["size"])
        if source_identity == target_identity:
            identical += 1
        else:
            changed.append(
                {"path": path, "source": source_item, "target": target_item}
            )

    transfer_candidates = missing_on_target + [item["source"] for item in changed]
    threshold = max(1, int(large_file_threshold))
    large_files = [item for item in transfer_candidates if item["size"] >= threshold]
    transfer_bytes = sum(item["size"] for item in transfer_candidates)
    return {
        "source_git_commit": source_manifest["git_commit"],
        "target_git_commit": target_manifest["git_commit"],
        "missing_on_target": missing_on_target,
        "missing_on_source": missing_on_source,
        "changed": changed,
        "identical_count": identical,
        "transfer_candidate_count": len(transfer_candidates),
        "transfer_bytes": transfer_bytes,
        "large_file_threshold": threshold,
        "large_files": large_files,
        "requires_large_transfer_confirmation": bool(large_files),
    }


def render_project_memory_markdown(
    project: dict[str, Any],
    memory: dict[str, Any] | None,
    handoffs: list[dict[str, Any]] | None = None,
) -> str:
    active = memory or {}
    lines = [
        f"# Project Memory: {project['name']}",
        "",
        f"Project slug: `{project['slug']}`",
        f"Memory version: {active.get('version') or 0}",
        f"Updated at: {active.get('created_at') or '(not set)'}",
        "",
    ]
    for heading, field in PROJECT_MEMORY_SECTIONS:
        lines.extend([f"## {heading}", str(active.get(field) or "(not set)"), ""])

    lines.extend(["## Recent Handoffs", ""])
    recent = list(handoffs or [])[:5]
    if not recent:
        lines.extend(["- (none)", ""])
    else:
        for handoff in recent:
            source = handoff.get("source_device_label") or "user"
            target = handoff.get("target_device_label") or "any device"
            lines.extend(
                [
                    f"### Handoff v{handoff.get('version')} · {source} → {target}",
                    f"Status: {handoff.get('status') or 'ready'}",
                    "",
                    str(handoff.get("summary_markdown") or "(empty)"),
                    "",
                ]
            )
    lines.extend(
        [
            "## Memory Use",
            "This document is the durable, model-neutral project memory. A new local AI conversation may be created for each item, but it must read this memory before acting and write durable conclusions back through the Hub handoff flow.",
            "",
        ]
    )
    return "\n".join(lines)


def render_runtime_project_memory_markdown(
    project: dict[str, Any], memory: dict[str, Any] | None
) -> str:
    """Render the small execution context; detailed facts stay in the local mirror."""

    active = memory or {}
    lines = [
        f"# Project Context: {project['name']}",
        f"Memory version: {active.get('version') or 0}",
    ]
    for heading, field in PROJECT_MEMORY_SECTIONS:
        if field not in RUNTIME_PROJECT_MEMORY_FIELDS:
            continue
        value = str(active.get(field) or "").strip()
        if not value:
            continue
        separator = "\n" if "\n" in value else " "
        lines.append(f"{heading}:{separator}{value}")
    lines.append(
        "Full memory: .codex-hub/PROJECT_MEMORY.md "
        "(read only when architecture, roles, files, or handoff history are needed)."
    )
    return "\n".join(lines) + "\n"


def _normalize_relative_path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise ValueError("manifest paths must be project-relative")
    path = PurePosixPath(raw)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("manifest paths must not traverse directories")
    normalized = path.as_posix()
    if len(normalized) > 500:
        raise ValueError("manifest path is too long")
    return normalized
