from __future__ import annotations

import json
from typing import Any


def build_task_prompt_markdown(
    *,
    project: dict[str, Any],
    control: dict[str, Any] | None,
    task: dict[str, Any],
    entries: list[dict[str, Any]],
    attachments: list[dict[str, Any]],
    primary_device: dict[str, Any] | None = None,
) -> str:
    primary_device_label = (primary_device or {}).get("label") or ""
    lines = [
        f"# Codex Task: {task['title']}",
        "",
        "## Project",
        f"- Name: {project['name']}",
        f"- Slug: {project['slug']}",
        f"- Description: {project.get('description') or '(none)'}",
        "",
        "## Shared Goal",
        (control or {}).get("goal") or "(not set)",
        "",
        "## Current Phase",
        (control or {}).get("current_phase") or "(not set)",
        "",
        "## Architecture",
        (control or {}).get("architecture") or "(not set)",
        "",
        "## Decisions",
        (control or {}).get("decisions") or "(not set)",
        "",
        "## Known Issues",
        (control or {}).get("known_issues") or "(not set)",
        "",
        "## User Preferences",
        (control or {}).get("user_preferences") or "(not set)",
        "",
        "## Key Files",
        (control or {}).get("key_files") or "(not set)",
        "",
        "## Project Next Steps",
        (control or {}).get("next_steps") or "(not set)",
        "",
        "## Constraints",
        (control or {}).get("constraints_text") or "(not set)",
        "",
        "## Acceptance",
        (control or {}).get("acceptance") or "(not set)",
        "",
        "## Task",
        f"- Status: {task['status']}",
        f"- Priority: {task['priority']}",
        f"- Assigned device: {task.get('assigned_device_label') or '(none)'}",
        f"- Claimed by: {task.get('claimed_device_label') or '(unclaimed)'}",
        f"- Requested group: {task.get('requested_group_name') or '(none)'}",
        f"- Primary device: @{primary_device_label}" if primary_device_label else "- Primary device: (not set)",
        "",
        task.get("description") or "(no task description)",
        "",
        "## Context Pack",
        task.get("context_pack_markdown") or "(empty)",
        "",
        "## Task Conversation",
    ]
    if entries:
        for entry in entries[-12:]:
            # The initial dispatch message is already included as the task body.
            # Keep later messages, including a repeated instruction from a device.
            if task.get('assigned_agent_id') and entry.get('author_type') == 'user' and (
                    entry.get('body_markdown') or '').strip() == (task.get('description') or '').strip():
                try:
                    metadata = json.loads(entry.get('metadata_json') or '{}')
                except (ValueError, TypeError):
                    metadata = {}
                if (isinstance(metadata, dict) and metadata.get('dispatch_id')
                        and isinstance(metadata.get('task_ids'), list) and task.get('id') in metadata['task_ids']):
                    continue
            lines.extend(
                [
                    f"### {entry['source_label']} / {entry['kind']} / {entry['created_at']}",
                    entry.get("body_markdown") or "",
                    "",
                ]
            )
    else:
        lines.append("(no task conversation yet)")
        lines.append("")

    lines.extend(["## Attachments", ""])
    if attachments:
        for attachment in attachments:
            lines.append(
                f"- {attachment['original_filename']} ({attachment['byte_size']} bytes)"
            )
    else:
        lines.append("- (none)")

    group_task = bool(task.get("requested_group_id")) and not task.get("assigned_device_id")
    lines.extend(["", "## Execution Scope"])
    if task.get("assigned_agent_kind") == "controller":
        lines.extend([
            "You are the durable controller assigned to this task. Within this task's goal, select appropriate existing agents and delegate concrete work using the Hub delegation helper.",
            "A successful delegation response means work was queued, not completed. Report task IDs and actual progress; never invent agent results.",
            "Reuse this work conversation for related subtasks. Do not expand to unrelated projects or repeat a dispatch after an uncertain response; retry with the same request_key.",
        ])
    elif group_task:
        lines.extend(
            [
                "This task explicitly requests the named device group.",
                "Contribute within that group; do not recruit devices outside the requested group.",
                "Evaluate other device outputs independently before adopting them.",
            ]
        )
    else:
        lines.extend(
            [
                "Handle this task directly as its selected or claimed executor.",
                "Return your result to the user in this Hub task; do not automatically mention or notify the project's primary device, other models, or other members.",
                "A project primary device or project collaboration preference does not expand this task's execution scope.",
            ]
        )

    lines.extend(
        [
            "",
            "## Required Return",
            "When finished, write a concise result summary back to this Hub task and mark the status as review or done.",
            "If this work changes durable project facts, decisions, known issues, key files, or next steps, propose a concise project-memory update or handoff instead of relying on this conversation alone.",
        ]
    )
    return "\n".join(lines)
