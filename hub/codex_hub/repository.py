from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .prompts import build_task_prompt_markdown as render_task_prompt_markdown
from .notifications.storage import NotificationStore
from .agents import AgentStore
from .execution_leases import ExecutionConflict, guard_legacy_task_write, latest_execution
from .project_memory import (
    DEFAULT_LARGE_FILE_THRESHOLD,
    HANDOFF_STATUS_VALUES,
    compare_project_manifests,
    normalize_project_manifest,
)
from .security import create_api_token as new_api_token
from .security import create_session_token, hash_password, hash_token, verify_password
from .text_safety import validate_device_entry_text, validate_project_memory_fields

PERMISSION_RANK = {"read": 1, "write": 2, "admin": 3}
TASK_STATUS_VALUES = {
    "open",
    "claimed",
    "in_progress",
    "blocked",
    "review",
    "done",
    "cancelled",
}
TASK_PRIORITY_VALUES = {"low", "normal", "high"}
MENTION_DELIVERY_STATUS_VALUES = {
    "claimed",
    "dispatching",
    "submitted",
    "handled",
}
SERVER_AI_JOB_STATUS_VALUES = {
    "awaiting_key",
    "queued",
    "running",
    "retrying",
    "succeeded",
    "failed",
}
MENTION_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_.-])@([^\s@]+)")
MENTION_TRAILING_PUNCTUATION = ".,;:!?，。；：！？)]}>'\""
CHAT_ROLE_NAME_RE = re.compile(r"^[\w.-]{1,40}$", re.UNICODE)


@dataclass(frozen=True, slots=True)
class StoredToken:
    id: int
    raw: str
    prefix: str


@dataclass(frozen=True, slots=True)
class StoredSession:
    raw: str
    csrf: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def extract_mention_tokens(value: str) -> set[str]:
    return {
        token.rstrip(MENTION_TRAILING_PUNCTUATION).casefold()
        for token in MENTION_TOKEN_RE.findall(value or "")
        if token.rstrip(MENTION_TRAILING_PUNCTUATION)
    }


def _mention_target_sets(
    body_markdown: str,
    device: dict[str, Any],
    role_names: list[str] | tuple[str, ...] = (),
) -> tuple[list[str], list[str]]:
    targets = sorted(extract_mention_tokens(body_markdown))
    device_targets = {str(device["label"]).casefold()}
    if device.get("group_name"):
        device_targets.add(str(device["group_name"]).casefold())
    device_targets.update(str(name).casefold() for name in role_names)
    matched = sorted(set(targets) & device_targets)
    return targets, matched


def entry_routing(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Read a message's immutable dispatch scope; absent means legacy mentions."""
    try:
        metadata = json.loads(entry.get("metadata_json") or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(metadata, dict) or "routing" not in metadata:
        return None
    routing = metadata["routing"]
    if (
        not isinstance(routing, dict)
        or routing.get("mode") not in {"selected", "group", "none"}
        or not isinstance(routing.get("device_ids"), list)
        or any(type(value) is not int or value <= 0 for value in routing.get("device_ids", []))
    ):
        return {"mode": "none", "device_ids": []}
    return routing


def _is_entry_delivery(
    entry: dict[str, Any], device: dict[str, Any], matched_targets: list[str]
) -> bool:
    routing = entry_routing(entry)
    if routing is None:
        # Project-wide switches never reinterpret or replay historical messages.
        return bool(matched_targets)
    return (
        routing.get("mode") in {"selected", "group"}
        and device.get("id") in routing.get("device_ids", [])
        and (
            routing.get("mode") != "group"
            or entry.get("project_permission") in {"write", "admin"}
        )
    )


def _is_collaboration_delivery(
    entry: dict[str, Any], device: dict[str, Any]
) -> bool:
    routing = entry_routing(entry) or {}
    return routing.get("mode") == "group" and _is_entry_delivery(entry, device, [])


def _annotate_delivery(
    entry: dict[str, Any],
    device: dict[str, Any],
    mention_targets: list[str],
    matched_targets: list[str],
    chat_roles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    collaboration_delivery = _is_collaboration_delivery(entry, device)
    routing = entry_routing(entry)
    entry["delivery_mode"] = (routing or {}).get("mode", "selected")
    entry["target_device_ids"] = (routing or {}).get("device_ids", [device["id"]])
    entry["mention_targets"] = mention_targets
    entry["matched_targets"] = matched_targets
    entry["delivery_reason"] = (
        "collaboration" if collaboration_delivery else "mention" if matched_targets else "selected"
    )
    primary_device_id = entry.get("project_primary_device_id")
    entry["collaboration_role"] = (
        "primary"
        if collaboration_delivery and primary_device_id == device.get("id")
        else "advisor"
        if collaboration_delivery
        else ""
    )
    entry["collaboration_enabled"] = collaboration_delivery
    entry["group_chat_roles"] = [
        {
            "id": role["id"],
            "name": role["name"],
            "instructions": role.get("instructions") or "",
            "device_id": role["device_id"],
            "device_label": role.get("device_label") or device.get("label") or "",
        }
        for role in (chat_roles or [])
        if collaboration_delivery or str(role["name"]).casefold() in matched_targets
    ]
    if routing and routing.get("mode") == "selected":
        # Older bridges unconditionally @ this label and inject group roles.
        # Clear their activation hints as well as publishing the new scope.
        entry["primary_device_label"] = ""
        entry["group_chat_roles"] = []
    return entry


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


class Repository:
    def __init__(
        self, connection: sqlite3.Connection, *, email_notifications_enabled: bool = False
    ):
        self.connection = connection
        self.email_notifications_enabled = email_notifications_enabled

    def create_owner_if_missing(
        self, username: str, password: str | None
    ) -> dict[str, Any] | None:
        existing = self.get_user_by_username(username)
        if existing:
            if password is not None:
                now = utc_now()
                self.connection.execute(
                    """
                    UPDATE users
                    SET password_hash = ?, role = 'owner', updated_at = ?
                    WHERE id = ?
                    """,
                    (hash_password(password), now, existing["id"]),
                )
                self.connection.commit()
                return self.get_user(existing["id"])
            return existing
        first_owner = self.connection.execute(
            "SELECT * FROM users WHERE role = 'owner' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if first_owner and password is not None:
            now = utc_now()
            self.connection.execute(
                """
                UPDATE users
                SET username = ?, password_hash = ?, role = 'owner', updated_at = ?
                WHERE id = ?
                """,
                (username, hash_password(password), now, first_owner["id"]),
            )
            self.connection.commit()
            return self.get_user(first_owner["id"])
        if password is None:
            return _row_to_dict(first_owner)
        now = utc_now()
        cursor = self.connection.execute(
            """
            INSERT INTO users (username, password_hash, role, created_at, updated_at)
            VALUES (?, ?, 'owner', ?, ?)
            """,
            (username, hash_password(password), now, now),
        )
        self.connection.commit()
        return self.get_user(cursor.lastrowid)

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        return _row_to_dict(
            self.connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        )

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        return _row_to_dict(
            self.connection.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
        )

    def authenticate_user(self, username: str, password: str) -> dict[str, Any] | None:
        user = self.get_user_by_username(username)
        if not user or not verify_password(password, user["password_hash"]):
            return None
        return user

    def create_session(self, user_id: int) -> StoredSession:
        raw = create_session_token()
        csrf = create_session_token()
        now = utc_now()
        expires_at = (
            datetime.now(timezone.utc) + timedelta(days=14)
        ).isoformat(timespec="seconds")
        self.connection.execute(
            """
            INSERT INTO sessions
              (user_id, session_hash, csrf_token_hash, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, hash_token(raw), hash_token(csrf), expires_at, now),
        )
        self.connection.commit()
        return StoredSession(raw=raw, csrf=csrf)

    def get_session_user(self, raw_session: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT users.*
            FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.session_hash = ? AND sessions.expires_at > ?
            """,
            (hash_token(raw_session), utc_now()),
        ).fetchone()
        return _row_to_dict(row)

    def session_matches_csrf(self, raw_session: str, raw_csrf: str) -> bool:
        if not raw_session or not raw_csrf:
            return False
        row = self.connection.execute(
            """
            SELECT 1
            FROM sessions
            WHERE session_hash = ?
              AND csrf_token_hash = ?
              AND expires_at > ?
            """,
            (hash_token(raw_session), hash_token(raw_csrf), utc_now()),
        ).fetchone()
        return row is not None

    def rotate_session_csrf(self, raw_session: str) -> str | None:
        csrf = create_session_token()
        cursor = self.connection.execute(
            """
            UPDATE sessions
            SET csrf_token_hash = ?
            WHERE session_hash = ? AND expires_at > ?
            """,
            (hash_token(csrf), hash_token(raw_session), utc_now()),
        )
        self.connection.commit()
        return csrf if cursor.rowcount == 1 else None

    def delete_session(self, raw_session: str) -> None:
        self.connection.execute(
            "DELETE FROM sessions WHERE session_hash = ?", (hash_token(raw_session),)
        )
        self.connection.commit()

    def create_group(self, name: str, description: str = "") -> dict[str, Any]:
        now = utc_now()
        cursor = self.connection.execute(
            """
            INSERT INTO device_groups (name, description, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (name.strip(), description.strip(), now, now),
        )
        self.connection.commit()
        return self.get_group(cursor.lastrowid)

    def get_group(self, group_id: int) -> dict[str, Any] | None:
        return _row_to_dict(
            self.connection.execute(
                "SELECT * FROM device_groups WHERE id = ?", (group_id,)
            ).fetchone()
        )

    def list_groups(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM device_groups ORDER BY name"
            ).fetchall()
        ]

    def create_device(
        self,
        label: str,
        group_id: int | None = None,
        note: str = "",
        server_ai_provider: str = "",
    ) -> dict[str, Any]:
        now = utc_now()
        cursor = self.connection.execute(
            """
            INSERT INTO devices
              (label, group_id, note, server_ai_provider, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                label.strip(),
                group_id or None,
                note.strip(),
                server_ai_provider.strip().casefold(),
                now,
                now,
            ),
        )
        self.connection.commit()
        return self.get_device(cursor.lastrowid)

    def get_server_ai_device(self, provider: str) -> dict[str, Any] | None:
        return _row_to_dict(
            self.connection.execute(
                """
                SELECT devices.*, device_groups.name AS group_name
                FROM devices
                LEFT JOIN device_groups ON device_groups.id = devices.group_id
                WHERE devices.server_ai_provider = ?
                """,
                (provider.strip().casefold(),),
            ).fetchone()
        )

    def ensure_server_ai_device(self, provider: str, label: str) -> dict[str, Any]:
        clean_provider = provider.strip().casefold()
        clean_label = label.strip()
        if not clean_provider or not clean_label:
            raise ValueError("server AI provider and label are required")
        existing = self.get_server_ai_device(clean_provider)
        now = utc_now()
        if existing:
            self.connection.execute(
                """
                UPDATE devices
                SET label = ?, note = ?, is_active = 1, updated_at = ?
                WHERE id = ?
                """,
                (
                    clean_label,
                    "Codex Hub server-managed AI; credentials stay in the server environment.",
                    now,
                    existing["id"],
                ),
            )
            self.connection.commit()
            return self.get_device(int(existing["id"]))
        label_owner = self.connection.execute(
            "SELECT id, server_ai_provider FROM devices WHERE label = ?",
            (clean_label,),
        ).fetchone()
        if label_owner is not None:
            raise ValueError(f"device label {clean_label!r} is already in use")
        return self.create_device(
            label=clean_label,
            note="Codex Hub server-managed AI; credentials stay in the server environment.",
            server_ai_provider=clean_provider,
        )

    def list_server_ai_devices(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT devices.*, device_groups.name AS group_name
                FROM devices
                LEFT JOIN device_groups ON device_groups.id = devices.group_id
                WHERE devices.server_ai_provider != ''
                ORDER BY devices.label COLLATE NOCASE
                """
            ).fetchall()
        ]

    def get_device(self, device_id: int) -> dict[str, Any] | None:
        return _row_to_dict(
            self.connection.execute(
                """
                SELECT devices.*, device_groups.name AS group_name
                FROM devices
                LEFT JOIN device_groups ON device_groups.id = devices.group_id
                WHERE devices.id = ?
                """,
                (device_id,),
            ).fetchone()
        )

    def list_devices(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT devices.*, device_groups.name AS group_name
                FROM devices
                LEFT JOIN device_groups ON device_groups.id = devices.group_id
                ORDER BY devices.label
                """
            ).fetchall()
        ]

    def touch_device(
        self, device_id: int, presence_source: str = "web"
    ) -> dict[str, Any] | None:
        if presence_source not in {"web", "agent"}:
            raise ValueError("invalid presence source")
        now = utc_now()
        source_column = (
            "last_agent_seen_at" if presence_source == "agent" else "last_web_seen_at"
        )
        cursor = self.connection.execute(
            f"""
            UPDATE devices
            SET last_seen_at = ?, {source_column} = ?, updated_at = ?
            WHERE id = ? AND is_active = 1
            """,
            (now, now, now, device_id),
        )
        self.connection.commit()
        if cursor.rowcount == 0:
            return None
        return self.get_device(device_id)

    def create_api_token(self, device_id: int, name: str) -> StoredToken:
        created = new_api_token()
        now = utc_now()
        cursor = self.connection.execute(
            """
            INSERT INTO api_tokens
              (device_id, token_prefix, token_hash, name, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (device_id, created.prefix, hash_token(created.raw), name.strip(), now),
        )
        self.connection.commit()
        return StoredToken(id=cursor.lastrowid, raw=created.raw, prefix=created.prefix)

    def list_tokens(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT api_tokens.id, api_tokens.device_id, api_tokens.token_prefix,
                       api_tokens.name, api_tokens.is_active, api_tokens.created_at,
                       api_tokens.last_used_at, api_tokens.revoked_at,
                       devices.label AS device_label
                FROM api_tokens
                JOIN devices ON devices.id = api_tokens.device_id
                ORDER BY api_tokens.created_at DESC
                """
            ).fetchall()
        ]

    def authenticate_api_token(self, raw_token: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT api_tokens.id AS token_id, api_tokens.name AS token_name,
                   devices.*, device_groups.name AS group_name
            FROM api_tokens
            JOIN devices ON devices.id = api_tokens.device_id
            LEFT JOIN device_groups ON device_groups.id = devices.group_id
            WHERE api_tokens.token_hash = ?
              AND api_tokens.is_active = 1
              AND devices.is_active = 1
            """,
            (hash_token(raw_token),),
        ).fetchone()
        if row is None:
            return None
        now = utc_now()
        self.connection.execute(
            "UPDATE api_tokens SET last_used_at = ? WHERE id = ?",
            (now, row["token_id"]),
        )
        self.touch_device(row["id"], presence_source="agent")
        self.connection.commit()
        return dict(row)

    def create_project(
        self,
        slug: str,
        name: str,
        description: str = "",
        created_by_user_id: int | None = None,
        created_by_device_id: int | None = None,
        primary_device_id: int | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        effective_primary_device_id = primary_device_id or created_by_device_id
        cursor = self.connection.execute(
            """
            INSERT INTO projects
              (slug, name, description, primary_device_id,
               created_by_user_id, created_by_device_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                slug.strip(),
                name.strip(),
                description.strip(),
                effective_primary_device_id,
                created_by_user_id,
                created_by_device_id,
                now,
                now,
            ),
        )
        project_id = cursor.lastrowid
        if created_by_device_id is not None:
            self.add_project_member(
                project_id=project_id,
                device_id=created_by_device_id,
                permission="write",
                added_by_user_id=created_by_user_id,
                added_by_device_id=created_by_device_id,
                commit=False,
            )
        self.connection.commit()
        return self.get_project(project_id)

    def get_project(self, project_id: int) -> dict[str, Any]:
        return dict(
            self.connection.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        )

    def get_project_by_slug(self, slug: str) -> dict[str, Any] | None:
        return _row_to_dict(
            self.connection.execute(
                "SELECT * FROM projects WHERE slug = ?", (slug,)
            ).fetchone()
        )

    def get_project_primary_device(self, project_id: int) -> dict[str, Any] | None:
        return _row_to_dict(
            self.connection.execute(
                """
                SELECT devices.*, device_groups.name AS group_name
                FROM projects
                JOIN devices ON devices.id = projects.primary_device_id
                LEFT JOIN device_groups ON device_groups.id = devices.group_id
                WHERE projects.id = ?
                """,
                (project_id,),
            ).fetchone()
        )

    def set_project_primary_device(
        self, project_id: int, device_id: int | None
    ) -> dict[str, Any]:
        if device_id is not None and not self.get_project_membership(
            project_id, device_id
        ):
            raise ValueError("primary device must be a project member")
        now = utc_now()
        self.connection.execute(
            """
            UPDATE projects
            SET primary_device_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (device_id, now, project_id),
        )
        self.connection.commit()
        return self.get_project(project_id)

    def set_project_collaboration_mode(
        self, project_id: int, enabled: bool
    ) -> dict[str, Any]:
        project = self.get_project(project_id)
        if enabled and not project.get("primary_device_id"):
            raise ValueError("collaboration mode requires a primary device")
        now = utc_now()
        started_at = (
            now
            if enabled and not bool(project.get("collaboration_enabled"))
            else project.get("collaboration_started_at")
        )
        start_entry_id = int(project.get("collaboration_start_entry_id") or 0)
        if enabled and not bool(project.get("collaboration_enabled")):
            row = self.connection.execute(
                "SELECT COALESCE(MAX(id), 0) AS last_id FROM entries WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            start_entry_id = int(row["last_id"])
        self.connection.execute(
            """
            UPDATE projects
            SET collaboration_enabled = ?,
                collaboration_started_at = ?,
                collaboration_start_entry_id = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (1 if enabled else 0, started_at, start_entry_id, now, project_id),
        )
        self.connection.commit()
        return self.get_project(project_id)

    def list_projects(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT projects.*,
                       COUNT(entries.id) AS entry_count
                FROM projects
                LEFT JOIN entries
                  ON entries.project_id = projects.id
                 AND entries.deleted_at IS NULL
                GROUP BY projects.id
                ORDER BY projects.updated_at DESC
                """
            ).fetchall()
        ]

    def list_projects_for_device(self, device_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT projects.*, project_memberships.permission
                FROM projects
                JOIN project_memberships
                  ON project_memberships.project_id = projects.id
                WHERE project_memberships.device_id = ?
                ORDER BY projects.updated_at DESC
                """,
                (device_id,),
            ).fetchall()
        ]

    def save_project_control(
        self,
        project_id: int,
        goal: str = "",
        non_goals: str = "",
        constraints_text: str = "",
        current_phase: str = "",
        acceptance: str = "",
        roles: str = "",
        decisions: str = "",
        risks: str = "",
        architecture: str = "",
        known_issues: str = "",
        user_preferences: str = "",
        key_files: str = "",
        next_steps: str = "",
        handoff_summary: str = "",
        created_by_user_id: int | None = None,
        created_by_device_id: int | None = None,
    ) -> dict[str, Any]:
        memory_fields = validate_project_memory_fields(
            {
                "goal": goal,
                "non_goals": non_goals,
                "constraints_text": constraints_text,
                "current_phase": current_phase,
                "acceptance": acceptance,
                "roles": roles,
                "decisions": decisions,
                "risks": risks,
                "architecture": architecture,
                "known_issues": known_issues,
                "user_preferences": user_preferences,
                "key_files": key_files,
                "next_steps": next_steps,
                "handoff_summary": handoff_summary,
            }
        )
        version = self.connection.execute(
            """
            SELECT COALESCE(MAX(version), 0) + 1 AS next_version
            FROM project_control_documents
            WHERE project_id = ?
            """,
            (project_id,),
        ).fetchone()["next_version"]
        cursor = self.connection.execute(
            """
            INSERT INTO project_control_documents
              (project_id, version, goal, non_goals, constraints_text,
               current_phase, acceptance, roles, decisions, risks,
               architecture, known_issues, user_preferences, key_files,
               next_steps, handoff_summary, created_by_user_id,
               created_by_device_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                version,
                memory_fields["goal"],
                memory_fields["non_goals"],
                memory_fields["constraints_text"],
                memory_fields["current_phase"],
                memory_fields["acceptance"],
                memory_fields["roles"],
                memory_fields["decisions"],
                memory_fields["risks"],
                memory_fields["architecture"],
                memory_fields["known_issues"],
                memory_fields["user_preferences"],
                memory_fields["key_files"],
                memory_fields["next_steps"],
                memory_fields["handoff_summary"],
                created_by_user_id,
                created_by_device_id,
                utc_now(),
            ),
        )
        self.connection.commit()
        return self.get_project_control(cursor.lastrowid)

    def get_project_control(self, control_id: int) -> dict[str, Any]:
        return dict(
            self.connection.execute(
                "SELECT * FROM project_control_documents WHERE id = ?",
                (control_id,),
            ).fetchone()
        )

    def get_latest_project_control(self, project_id: int) -> dict[str, Any] | None:
        return _row_to_dict(
            self.connection.execute(
                """
                SELECT *
                FROM project_control_documents
                WHERE project_id = ?
                ORDER BY version DESC
                LIMIT 1
                """,
                (project_id,),
            ).fetchone()
        )

    def create_task(
        self,
        project_id: int,
        title: str,
        description: str = "",
        status: str = "open",
        priority: str = "normal",
        assigned_device_id: int | None = None,
        requested_group_id: int | None = None,
        created_by_user_id: int | None = None,
        created_by_device_id: int | None = None,
        context_pack_markdown: str = "",
        result_summary: str = "",
        due_at: str | None = None,
        assigned_agent_id: int | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        if status not in TASK_STATUS_VALUES:
            raise ValueError("invalid task status")
        if priority not in TASK_PRIORITY_VALUES:
            raise ValueError("invalid task priority")
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("task title is required")
        if assigned_agent_id is not None:
            agent = AgentStore(self.connection).assignment(project_id, assigned_agent_id)
            assigned_device_id = agent["device_id"]
            requested_group_id = None
        if assigned_device_id is not None and not self.get_project_membership(
            project_id, assigned_device_id
        ):
            raise ValueError("assigned device is not on project allowlist")
        now = utc_now()
        cursor = self.connection.execute(
            """
            INSERT INTO tasks
              (project_id, title, description, status, priority,
               assigned_device_id, requested_group_id, created_by_user_id,
               created_by_device_id, context_pack_markdown, result_summary,
               due_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                clean_title,
                description.strip(),
                status,
                priority,
                assigned_device_id,
                requested_group_id,
                created_by_user_id,
                created_by_device_id,
                context_pack_markdown.strip(),
                result_summary.strip(),
                due_at,
                now,
                now,
            ),
        )
        self.connection.execute(
            "UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id)
        )
        if assigned_agent_id is not None:
            self.connection.execute("UPDATE tasks SET assigned_agent_id=? WHERE id=?",
                                    (assigned_agent_id, cursor.lastrowid))
        if commit:
            self.connection.commit()
        return self.get_task(cursor.lastrowid)

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        return _row_to_dict(
            self.connection.execute(
                """
                SELECT tasks.*,
                       projects.slug AS project_slug,
                       projects.name AS project_name,
                       assigned.label AS assigned_device_label,
                       claimed.label AS claimed_device_label,
                       device_groups.name AS requested_group_name
                FROM tasks
                JOIN projects ON projects.id = tasks.project_id
                LEFT JOIN devices AS assigned ON assigned.id = tasks.assigned_device_id
                LEFT JOIN devices AS claimed ON claimed.id = tasks.claimed_by_device_id
                LEFT JOIN device_groups ON device_groups.id = tasks.requested_group_id
                WHERE tasks.id = ?
                """,
                (task_id,),
            ).fetchone()
        )

    def list_tasks(
        self, project_id: int, include_done: bool = True
    ) -> list[dict[str, Any]]:
        status_filter = "" if include_done else "AND tasks.status NOT IN ('done', 'cancelled')"
        return [
            dict(row)
            for row in self.connection.execute(
                f"""
                SELECT tasks.*,
                       assigned.label AS assigned_device_label,
                       claimed.label AS claimed_device_label,
                       device_groups.name AS requested_group_name
                FROM tasks
                LEFT JOIN devices AS assigned ON assigned.id = tasks.assigned_device_id
                LEFT JOIN devices AS claimed ON claimed.id = tasks.claimed_by_device_id
                LEFT JOIN device_groups ON device_groups.id = tasks.requested_group_id
                WHERE tasks.project_id = ?
                {status_filter}
                ORDER BY
                  CASE tasks.priority
                    WHEN 'high' THEN 0
                    WHEN 'normal' THEN 1
                    ELSE 2
                  END,
                  tasks.updated_at DESC,
                  tasks.id DESC
                """,
                (project_id,),
            ).fetchall()
        ]

    def claim_task(self, task_id: int, device_id: int) -> dict[str, Any] | None:
        task = self.get_task(task_id)
        if not task or not self.can_device_write(task["project_id"], device_id):
            return None
        if task["claimed_by_device_id"] not in (None, device_id):
            return None
        if task["status"] in {"done", "cancelled"}:
            return None
        if task.get("assigned_agent_id"):
            execution = latest_execution(self.connection, task_id)
            if execution and execution['state'] in {'prepared','running','uncertain'}:
                # Expiry before start is safe to arbitrate at the acquire API;
                # a started or ambiguous execution is never a fresh inbox job.
                import time
                if execution['state'] != 'prepared' or execution['lease_until'] > time.time():
                    return None
            agent = AgentStore(self.connection).get(task["project_id"], task["assigned_agent_id"])
            if not agent["active"] or task["assigned_device_id"] != device_id:
                return None
        now = utc_now()
        cursor = self.connection.execute(
            """
            UPDATE tasks
            SET claimed_by_device_id = ?,
                status_version = status_version + CASE WHEN status = 'open' THEN 1 ELSE 0 END,
                status = CASE WHEN status = 'open' THEN 'claimed' ELSE status END,
                updated_at = ?
            WHERE id = ?
              AND (claimed_by_device_id IS NULL OR claimed_by_device_id = ?)
              AND status NOT IN ('done', 'cancelled')
              AND (assigned_agent_id IS NULL OR NOT EXISTS
                   (SELECT 1 FROM hub_task_executions WHERE task_id = tasks.id))
            """,
            (device_id, now, task_id, device_id),
        )
        if cursor.rowcount == 0:
            return None
        self.connection.commit()
        return self.get_task(task_id)

    def update_task_status(
        self,
        task_id: int,
        status: str,
        result_summary: str = "",
        reflection: dict | None = None,
        *,
        actor_device_id: int | None = None,
        execution_fenced: bool = False,
    ) -> dict[str, Any]:
        if status not in TASK_STATUS_VALUES:
            raise ValueError("invalid task status")
        now = utc_now()
        notifications = NotificationStore(self.connection)
        with notifications.transaction():
            task = self.connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not execution_fenced:
                guard_legacy_task_write(self.connection, dict(task) if task else None, actor_device_id)
            self.connection.execute(
                """
                UPDATE tasks
                SET status_version = status_version + CASE WHEN status != ? THEN 1 ELSE 0 END,
                    status = ?,
                    result_summary = COALESCE(NULLIF(?, ''), result_summary),
                    updated_at = ?
                WHERE id = ?
                """,
                (status, status, result_summary.strip(), now, task_id),
            )
            if task and task["status"] != status and status in {"done", "blocked"}:
                notifications.capture_event(
                    event_type="task." + status, entity_type="task", entity_id=task_id,
                    status_version=task["status_version"] + 1,
                    project_id=task["project_id"], occurred_at=now,
                    deployment_enabled=self.email_notifications_enabled,
                )
            updated = self.get_task(task_id)
            if updated:
                AgentStore(self.connection).record_outcome(updated, reflection)
        return self.get_task(task_id)

    def list_inbox_tasks_for_device(self, device_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT tasks.*,
                       (SELECT kind FROM hub_agents WHERE id=tasks.assigned_agent_id) AS assigned_agent_kind,
                       projects.slug AS project_slug,
                       projects.name AS project_name,
                       primary_device.label AS primary_device_label,
                       project_memberships.permission AS project_permission,
                       assigned.label AS assigned_device_label,
                       claimed.label AS claimed_device_label,
                       device_groups.name AS requested_group_name
                FROM tasks
                JOIN projects ON projects.id = tasks.project_id
                JOIN project_memberships
                  ON project_memberships.project_id = tasks.project_id
                 AND project_memberships.device_id = ?
                JOIN devices AS inbox_device ON inbox_device.id = ?
                LEFT JOIN devices AS assigned ON assigned.id = tasks.assigned_device_id
                LEFT JOIN devices AS claimed ON claimed.id = tasks.claimed_by_device_id
                LEFT JOIN devices AS primary_device ON primary_device.id = projects.primary_device_id
                LEFT JOIN device_groups ON device_groups.id = tasks.requested_group_id
                WHERE tasks.status NOT IN ('done', 'cancelled')
                  AND (tasks.wait_for_task_id IS NULL OR NOT EXISTS (
                    SELECT 1 FROM tasks parent WHERE parent.id=tasks.wait_for_task_id AND
                      (parent.status NOT IN ('review','done','blocked','cancelled') OR EXISTS
                       (SELECT 1 FROM hub_task_executions ex WHERE ex.task_id=parent.id AND ex.state!='completed'
                        AND ex.generation=(SELECT MAX(last.generation) FROM hub_task_executions last WHERE last.task_id=parent.id)))))
                  AND (
                    tasks.assigned_device_id = ?
                    OR tasks.claimed_by_device_id = ?
                    OR (
                      tasks.requested_group_id IS NOT NULL
                      AND tasks.requested_group_id = inbox_device.group_id
                    )
                    OR (
                      tasks.assigned_device_id IS NULL
                      AND tasks.requested_group_id IS NULL
                    )
                  )
                ORDER BY
                  CASE tasks.priority
                    WHEN 'high' THEN 0
                    WHEN 'normal' THEN 1
                    ELSE 2
                  END,
                  tasks.updated_at DESC,
                  tasks.id DESC
                """,
                (device_id, device_id, device_id, device_id),
            ).fetchall()
        ]

    def list_mentions_for_device(
        self, device_id: int, limit: int = 20, include_handled: bool = False
    ) -> list[dict[str, Any]]:
        device = self.get_device(device_id)
        if not device or not device.get("is_active"):
            return []
        mention_patterns = [f"%@{escape_like(device['label'])}%"]
        if device.get("group_name"):
            mention_patterns.append(f"%@{escape_like(device['group_name'])}%")
        mention_patterns.extend(
            f"%@{escape_like(role_name)}%"
            for role_name in self.list_chat_role_names_for_device(device_id)
        )

        mention_filter = " OR ".join(
            "entries.body_markdown LIKE ? ESCAPE '\\'" for _ in mention_patterns
        )
        params: list[Any] = [device_id, device_id, device_id, *mention_patterns, device_id]
        rows = self.connection.execute(
            f"""
            SELECT entries.*,
                   projects.slug AS project_slug,
                   projects.name AS project_name,
                   projects.primary_device_id AS project_primary_device_id,
                   projects.collaboration_enabled AS collaboration_enabled,
                   projects.collaboration_started_at AS collaboration_started_at,
                   projects.collaboration_start_entry_id AS collaboration_start_entry_id,
                   primary_device.label AS primary_device_label,
                   project_memberships.permission AS project_permission,
                   mention_receipts.status AS receipt_status,
                   mention_receipts.delivery_id AS receipt_delivery_id,
                   mention_receipts.claimed_at AS receipt_claimed_at,
                   mention_receipts.dispatching_at AS receipt_dispatching_at,
                   mention_receipts.submitted_at AS receipt_submitted_at,
                   mention_receipts.handled_at AS receipt_handled_at
            FROM entries
            JOIN projects ON projects.id = entries.project_id
            LEFT JOIN devices AS primary_device ON primary_device.id = projects.primary_device_id
            JOIN project_memberships
              ON project_memberships.project_id = entries.project_id
             AND project_memberships.device_id = ?
            LEFT JOIN mention_receipts
              ON mention_receipts.entry_id = entries.id
             AND mention_receipts.device_id = ?
            WHERE entries.deleted_at IS NULL
              AND COALESCE(entries.author_device_id, -1) != ?
              AND (
                ({mention_filter})
                OR EXISTS (
                  SELECT 1 FROM json_each(
                    CASE WHEN json_valid(entries.metadata_json)
                      THEN entries.metadata_json ELSE '{{}}' END,
                    '$.routing.device_ids'
                  ) AS routed_device
                  WHERE routed_device.value = ?
                )
              )
            ORDER BY entries.created_at DESC, entries.id DESC
            """,
            params,
        ).fetchall()

        mentions: list[dict[str, Any]] = []
        roles_by_project: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            mention = dict(row)
            project_id = int(row["project_id"])
            chat_roles = roles_by_project.get(project_id)
            if chat_roles is None:
                chat_roles = self.list_project_chat_roles(
                    project_id, device_id=device_id
                )
                roles_by_project[project_id] = chat_roles
            mention_targets, matched_targets = _mention_target_sets(
                row["body_markdown"],
                device,
                [role["name"] for role in chat_roles],
            )
            if not _is_entry_delivery(mention, device, matched_targets):
                continue
            claim_status = row["receipt_status"] or "unclaimed"
            if claim_status == "handled" and not include_handled:
                continue
            _annotate_delivery(
                mention,
                device,
                mention_targets,
                matched_targets,
                chat_roles,
            )
            mention["claim_status"] = claim_status
            mention["delivery_id"] = row["receipt_delivery_id"]
            mention["claimed_at"] = row["receipt_claimed_at"]
            mention["dispatching_at"] = row["receipt_dispatching_at"]
            mention["submitted_at"] = row["receipt_submitted_at"]
            mention["handled_at"] = row["receipt_handled_at"]
            mention.pop("receipt_status", None)
            mention.pop("receipt_delivery_id", None)
            mention.pop("receipt_claimed_at", None)
            mention.pop("receipt_dispatching_at", None)
            mention.pop("receipt_submitted_at", None)
            mention.pop("receipt_handled_at", None)
            mentions.append(mention)
            if len(mentions) >= limit:
                break
        return mentions

    def create_project_handoff(
        self,
        project_id: int,
        *,
        title: str = "",
        summary_markdown: str = "",
        status: str = "ready",
        base_memory_version: int | None = None,
        source_device_id: int | None = None,
        target_device_id: int | None = None,
        manifest: Any = None,
        created_by_user_id: int | None = None,
        created_by_device_id: int | None = None,
    ) -> dict[str, Any]:
        clean_status = str(status or "ready").strip().lower()
        if clean_status not in HANDOFF_STATUS_VALUES:
            raise ValueError("invalid handoff status")
        if source_device_id is not None and not self.get_project_membership(
            project_id, source_device_id
        ):
            raise ValueError("source device is not a project member")
        if target_device_id is not None and not self.get_project_membership(
            project_id, target_device_id
        ):
            raise ValueError("target device is not a project member")
        normalized = normalize_project_manifest(manifest)
        memory = self.get_latest_project_control(project_id)
        memory_version = (
            int(base_memory_version)
            if base_memory_version is not None
            else int((memory or {}).get("version") or 0)
        )
        version = self.connection.execute(
            """
            SELECT COALESCE(MAX(version), 0) + 1 AS next_version
            FROM project_handoffs
            WHERE project_id = ?
            """,
            (project_id,),
        ).fetchone()["next_version"]
        transfer_bytes = sum(item["size"] for item in normalized["files"])
        large_file_count = sum(
            1
            for item in normalized["files"]
            if item["size"] >= DEFAULT_LARGE_FILE_THRESHOLD
        )
        now = utc_now()
        cursor = self.connection.execute(
            """
            INSERT INTO project_handoffs
              (project_id, version, title, summary_markdown, status,
               base_memory_version, source_device_id, target_device_id,
               manifest_json, transfer_bytes, large_file_count,
               created_by_user_id, created_by_device_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                version,
                title.strip() or f"Project handoff v{version}",
                summary_markdown.strip(),
                clean_status,
                memory_version,
                source_device_id,
                target_device_id,
                json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
                transfer_bytes,
                large_file_count,
                created_by_user_id,
                created_by_device_id,
                now,
            ),
        )
        self.connection.commit()
        return self.get_project_handoff(cursor.lastrowid)

    def get_project_handoff(self, handoff_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT project_handoffs.*,
                   source.label AS source_device_label,
                   target.label AS target_device_label
            FROM project_handoffs
            LEFT JOIN devices AS source ON source.id = project_handoffs.source_device_id
            LEFT JOIN devices AS target ON target.id = project_handoffs.target_device_id
            WHERE project_handoffs.id = ?
            """,
            (handoff_id,),
        ).fetchone()
        if row is None:
            raise ValueError("handoff not found")
        handoff = dict(row)
        handoff["manifest"] = normalize_project_manifest(handoff["manifest_json"])
        return handoff

    def list_project_handoffs(
        self, project_id: int, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT project_handoffs.*,
                   source.label AS source_device_label,
                   target.label AS target_device_label
            FROM project_handoffs
            LEFT JOIN devices AS source ON source.id = project_handoffs.source_device_id
            LEFT JOIN devices AS target ON target.id = project_handoffs.target_device_id
            WHERE project_handoffs.project_id = ?
            ORDER BY project_handoffs.version DESC
            LIMIT ?
            """,
            (project_id, max(1, min(int(limit), 200))),
        ).fetchall()
        handoffs = [dict(row) for row in rows]
        for handoff in handoffs:
            handoff["manifest"] = normalize_project_manifest(handoff["manifest_json"])
        return handoffs

    def accept_project_handoff(
        self,
        project_id: int,
        handoff_id: int,
        *,
        accepted_by_user_id: int | None = None,
        accepted_by_device_id: int | None = None,
    ) -> dict[str, Any]:
        handoff = self.get_project_handoff(handoff_id)
        if handoff["project_id"] != project_id:
            raise ValueError("handoff not found")
        now = utc_now()
        self.connection.execute(
            """
            UPDATE project_handoffs
            SET status = 'accepted', accepted_by_user_id = ?,
                accepted_by_device_id = ?, accepted_at = ?
            WHERE id = ?
            """,
            (accepted_by_user_id, accepted_by_device_id, now, handoff_id),
        )
        self.connection.commit()
        return self.get_project_handoff(handoff_id)

    def save_project_sync_snapshot(
        self,
        project_id: int,
        device_id: int,
        *,
        memory_version: int = 0,
        git_commit: str = "",
        manifest: Any = None,
    ) -> dict[str, Any]:
        if not self.get_project_membership(project_id, device_id):
            raise ValueError("device is not a project member")
        normalized = normalize_project_manifest(manifest)
        commit = str(git_commit or normalized.get("git_commit") or "").strip()[:160]
        normalized["git_commit"] = commit
        revision = self.connection.execute(
            """
            SELECT COALESCE(MAX(revision), 0) + 1 AS next_revision
            FROM project_sync_snapshots
            WHERE project_id = ? AND device_id = ?
            """,
            (project_id, device_id),
        ).fetchone()["next_revision"]
        cursor = self.connection.execute(
            """
            INSERT INTO project_sync_snapshots
              (project_id, device_id, revision, memory_version, git_commit,
               manifest_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                device_id,
                revision,
                max(0, int(memory_version or 0)),
                commit,
                json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
                utc_now(),
            ),
        )
        self.connection.commit()
        return self.get_project_sync_snapshot(cursor.lastrowid)

    def get_project_sync_snapshot(self, snapshot_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT project_sync_snapshots.*, devices.label AS device_label
            FROM project_sync_snapshots
            JOIN devices ON devices.id = project_sync_snapshots.device_id
            WHERE project_sync_snapshots.id = ?
            """,
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise ValueError("sync snapshot not found")
        snapshot = dict(row)
        snapshot["manifest"] = normalize_project_manifest(snapshot["manifest_json"])
        return snapshot

    def get_latest_project_sync_snapshot(
        self, project_id: int, device_id: int
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT project_sync_snapshots.*, devices.label AS device_label
            FROM project_sync_snapshots
            JOIN devices ON devices.id = project_sync_snapshots.device_id
            WHERE project_sync_snapshots.project_id = ?
              AND project_sync_snapshots.device_id = ?
            ORDER BY project_sync_snapshots.revision DESC
            LIMIT 1
            """,
            (project_id, device_id),
        ).fetchone()
        if row is None:
            return None
        snapshot = dict(row)
        snapshot["manifest"] = normalize_project_manifest(snapshot["manifest_json"])
        return snapshot

    def list_latest_project_sync_snapshots(
        self, project_id: int
    ) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT snapshots.*, devices.label AS device_label
            FROM project_sync_snapshots AS snapshots
            JOIN devices ON devices.id = snapshots.device_id
            JOIN (
              SELECT device_id, MAX(revision) AS revision
              FROM project_sync_snapshots
              WHERE project_id = ?
              GROUP BY device_id
            ) AS latest
              ON latest.device_id = snapshots.device_id
             AND latest.revision = snapshots.revision
            WHERE snapshots.project_id = ?
            ORDER BY devices.label
            """,
            (project_id, project_id),
        ).fetchall()
        snapshots = [dict(row) for row in rows]
        for snapshot in snapshots:
            snapshot["manifest"] = normalize_project_manifest(snapshot["manifest_json"])
        return snapshots

    def compare_project_devices(
        self, project_id: int, source_device_id: int, target_device_id: int
    ) -> dict[str, Any]:
        source = self.get_latest_project_sync_snapshot(project_id, source_device_id)
        target = self.get_latest_project_sync_snapshot(project_id, target_device_id)
        if source is None or target is None:
            raise ValueError("both devices need a sync snapshot")
        comparison = compare_project_manifests(source["manifest"], target["manifest"])
        comparison.update(
            {
                "source_device_id": source_device_id,
                "source_device_label": source["device_label"],
                "source_revision": source["revision"],
                "source_memory_version": source["memory_version"],
                "target_device_id": target_device_id,
                "target_device_label": target["device_label"],
                "target_revision": target["revision"],
                "target_memory_version": target["memory_version"],
            }
        )
        return comparison

    def get_mention_receipt(
        self, entry_id: int, device_id: int
    ) -> dict[str, Any] | None:
        return _row_to_dict(
            self.connection.execute(
                """
                SELECT mention_receipts.*, devices.label AS device_label
                FROM mention_receipts
                JOIN devices ON devices.id = mention_receipts.device_id
                WHERE mention_receipts.entry_id = ?
                  AND mention_receipts.device_id = ?
                """,
                (entry_id, device_id),
            ).fetchone()
        )

    def get_mention_for_device(
        self, entry_id: int, device_id: int, include_handled: bool = True
    ) -> dict[str, Any] | None:
        device = self.get_device(device_id)
        if not device or not device.get("is_active"):
            return None
        row = self.connection.execute(
            """
            SELECT entries.*,
                   projects.slug AS project_slug,
                   projects.name AS project_name,
                   projects.primary_device_id AS project_primary_device_id,
                   projects.collaboration_enabled AS collaboration_enabled,
                   projects.collaboration_started_at AS collaboration_started_at,
                   projects.collaboration_start_entry_id AS collaboration_start_entry_id,
                   primary_device.label AS primary_device_label,
                   project_memberships.permission AS project_permission,
                   mention_receipts.status AS receipt_status,
                   mention_receipts.delivery_id AS receipt_delivery_id,
                   mention_receipts.claimed_at AS receipt_claimed_at,
                   mention_receipts.dispatching_at AS receipt_dispatching_at,
                   mention_receipts.submitted_at AS receipt_submitted_at,
                   mention_receipts.handled_at AS receipt_handled_at
            FROM entries
            JOIN projects ON projects.id = entries.project_id
            LEFT JOIN devices AS primary_device ON primary_device.id = projects.primary_device_id
            JOIN project_memberships
              ON project_memberships.project_id = entries.project_id
             AND project_memberships.device_id = ?
            LEFT JOIN mention_receipts
              ON mention_receipts.entry_id = entries.id
             AND mention_receipts.device_id = ?
            WHERE entries.id = ?
              AND entries.deleted_at IS NULL
              AND COALESCE(entries.author_device_id, -1) != ?
            """,
            (device_id, device_id, entry_id, device_id),
        ).fetchone()
        if not row:
            return None
        mention = dict(row)
        chat_roles = self.list_project_chat_roles(
            int(row["project_id"]), device_id=device_id
        )
        mention_targets, matched_targets = _mention_target_sets(
            row["body_markdown"],
            device,
            [role["name"] for role in chat_roles],
        )
        if not _is_entry_delivery(mention, device, matched_targets):
            return None
        claim_status = row["receipt_status"] or "unclaimed"
        if claim_status == "handled" and not include_handled:
            return None
        _annotate_delivery(
            mention,
            device,
            mention_targets,
            matched_targets,
            chat_roles,
        )
        mention["claim_status"] = claim_status
        mention["delivery_id"] = row["receipt_delivery_id"]
        mention["claimed_at"] = row["receipt_claimed_at"]
        mention["dispatching_at"] = row["receipt_dispatching_at"]
        mention["submitted_at"] = row["receipt_submitted_at"]
        mention["handled_at"] = row["receipt_handled_at"]
        mention.pop("receipt_status", None)
        mention.pop("receipt_delivery_id", None)
        mention.pop("receipt_claimed_at", None)
        mention.pop("receipt_dispatching_at", None)
        mention.pop("receipt_submitted_at", None)
        mention.pop("receipt_handled_at", None)
        return mention

    def claim_mention(self, entry_id: int, device_id: int) -> dict[str, Any] | None:
        if not self.get_mention_for_device(entry_id, device_id, include_handled=True):
            return None
        existing = self.get_mention_receipt(entry_id, device_id)
        if existing:
            if existing["status"] == "handled":
                return None
            if not existing.get("delivery_id"):
                now = utc_now()
                self.connection.execute(
                    """
                    UPDATE mention_receipts
                    SET delivery_id = ?, updated_at = ?
                    WHERE entry_id = ? AND device_id = ?
                    """,
                    (uuid.uuid4().hex, now, entry_id, device_id),
                )
                self.connection.commit()
                return self.get_mention_receipt(entry_id, device_id)
            return existing
        now = utc_now()
        delivery_id = uuid.uuid4().hex
        self.connection.execute(
            """
            INSERT INTO mention_receipts
              (entry_id, device_id, status, delivery_id, claimed_at,
               created_at, updated_at)
            VALUES (?, ?, 'claimed', ?, ?, ?, ?)
            """,
            (entry_id, device_id, delivery_id, now, now, now),
        )
        self.connection.commit()
        return self.get_mention_receipt(entry_id, device_id)

    def mark_mention_dispatching(
        self, entry_id: int, device_id: int
    ) -> dict[str, Any] | None:
        return self._advance_mention_delivery(entry_id, device_id, "dispatching")

    def mark_mention_submitted(
        self, entry_id: int, device_id: int
    ) -> dict[str, Any] | None:
        return self._advance_mention_delivery(entry_id, device_id, "submitted")

    def release_mention_dispatch(
        self, entry_id: int, device_id: int, last_error: str = ""
    ) -> dict[str, Any] | None:
        existing = self.get_mention_receipt(entry_id, device_id)
        if not existing:
            return None
        if existing["status"] != "dispatching":
            return existing
        now = utc_now()
        self.connection.execute(
            """
            UPDATE mention_receipts
            SET status = 'claimed',
                dispatching_at = NULL,
                last_error = ?,
                updated_at = ?
            WHERE entry_id = ? AND device_id = ? AND status = 'dispatching'
            """,
            (str(last_error), now, entry_id, device_id),
        )
        self.connection.commit()
        return self.get_mention_receipt(entry_id, device_id)

    def _advance_mention_delivery(
        self, entry_id: int, device_id: int, target_status: str
    ) -> dict[str, Any] | None:
        if target_status not in MENTION_DELIVERY_STATUS_VALUES:
            raise ValueError("invalid mention delivery status")
        if not self.get_mention_for_device(entry_id, device_id, include_handled=True):
            return None
        existing = self.get_mention_receipt(entry_id, device_id)
        if not existing:
            existing = self.claim_mention(entry_id, device_id)
        if not existing:
            return None
        rank = {"claimed": 0, "dispatching": 1, "submitted": 2, "handled": 3}
        if rank.get(str(existing["status"]), -1) >= rank[target_status]:
            return existing
        now = utc_now()
        timestamp_column = {
            "dispatching": "dispatching_at",
            "submitted": "submitted_at",
            "handled": "handled_at",
        }.get(target_status)
        if not timestamp_column:
            return existing
        self.connection.execute(
            f"""
            UPDATE mention_receipts
            SET status = ?,
                {timestamp_column} = COALESCE({timestamp_column}, ?),
                last_error = '',
                updated_at = ?
            WHERE entry_id = ? AND device_id = ?
            """,
            (target_status, now, now, entry_id, device_id),
        )
        self.connection.commit()
        return self.get_mention_receipt(entry_id, device_id)

    def mark_mention_handled(
        self, entry_id: int, device_id: int
    ) -> dict[str, Any] | None:
        if not self.get_mention_for_device(entry_id, device_id, include_handled=True):
            return None
        return self._advance_mention_delivery(entry_id, device_id, "handled")

    def enqueue_server_ai_jobs_for_entry(
        self,
        entry_id: int,
        configured_providers: set[str] | frozenset[str] = frozenset(),
        provider_models: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        entry = self.get_entry(entry_id)
        if entry.get("author_type") != "user":
            return []
        mention_targets = extract_mention_tokens(str(entry.get("body_markdown") or ""))
        routing = entry_routing(entry)
        if not mention_targets and not (routing and routing.get("device_ids")):
            return []
        configured = {item.strip().casefold() for item in configured_providers}
        models = {
            str(key).strip().casefold(): str(value).strip()
            for key, value in (provider_models or {}).items()
        }
        devices = [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT devices.*, project_memberships.permission
                FROM project_memberships
                JOIN devices ON devices.id = project_memberships.device_id
                WHERE project_memberships.project_id = ?
                  AND project_memberships.permission IN ('write', 'admin')
                  AND devices.is_active = 1
                  AND devices.server_ai_provider != ''
                ORDER BY devices.id
                """,
                (entry["project_id"],),
            ).fetchall()
        ]
        role_names_by_device: dict[int, set[str]] = {}
        for role in self.list_project_chat_roles(int(entry["project_id"])):
            role_names_by_device.setdefault(int(role["device_id"]), set()).add(
                str(role["name"]).casefold()
            )
        now = utc_now()
        job_ids: list[int] = []
        for device in devices:
            provider = str(device["server_ai_provider"]).casefold()
            accepted_targets = {str(device["label"]).casefold()}
            accepted_targets.update(role_names_by_device.get(int(device["id"]), set()))
            device_entry = {**entry, "project_permission": device["permission"]}
            if not _is_entry_delivery(
                device_entry, device, sorted(mention_targets & accepted_targets)
            ):
                continue
            status = "queued" if provider in configured else "awaiting_key"
            cursor = self.connection.execute(
                """
                INSERT INTO server_ai_jobs
                  (project_id, source_entry_id, target_device_id, provider,
                   model, status, next_attempt_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_entry_id, target_device_id) DO NOTHING
                """,
                (
                    entry["project_id"],
                    entry["id"],
                    device["id"],
                    provider,
                    models.get(provider, ""),
                    status,
                    now if status == "queued" else None,
                    now,
                    now,
                ),
            )
            if cursor.rowcount:
                job_ids.append(int(cursor.lastrowid))
            else:
                existing = self.connection.execute(
                    """
                    SELECT id FROM server_ai_jobs
                    WHERE source_entry_id = ? AND target_device_id = ?
                    """,
                    (entry["id"], device["id"]),
                ).fetchone()
                if existing is not None:
                    job_ids.append(int(existing["id"]))
        self.connection.commit()
        return [self.get_server_ai_job(job_id) for job_id in job_ids]

    def get_server_ai_job(self, job_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT server_ai_jobs.*,
                   projects.slug AS project_slug,
                   projects.name AS project_name,
                   entries.body_markdown AS source_body_markdown,
                   entries.task_id AS source_task_id,
                   devices.label AS target_device_label,
                   devices.server_ai_provider AS target_server_ai_provider
            FROM server_ai_jobs
            JOIN projects ON projects.id = server_ai_jobs.project_id
            JOIN entries ON entries.id = server_ai_jobs.source_entry_id
            JOIN devices ON devices.id = server_ai_jobs.target_device_id
            WHERE server_ai_jobs.id = ?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            raise ValueError("server AI job not found")
        return dict(row)

    def list_server_ai_jobs(
        self, project_id: int, limit: int = 20
    ) -> list[dict[str, Any]]:
        clean_limit = min(100, max(1, int(limit)))
        return [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT server_ai_jobs.*,
                       devices.label AS target_device_label
                FROM server_ai_jobs
                JOIN devices ON devices.id = server_ai_jobs.target_device_id
                WHERE server_ai_jobs.project_id = ?
                ORDER BY server_ai_jobs.id DESC
                LIMIT ?
                """,
                (project_id, clean_limit),
            ).fetchall()
        ]

    def count_server_ai_attempts_since(self, provider: str, since: str) -> int:
        row = self.connection.execute(
            """
            SELECT COALESCE(SUM(attempts), 0) AS attempt_count
            FROM server_ai_jobs
            WHERE provider = ? AND created_at >= ?
            """,
            (provider.strip().casefold(), since),
        ).fetchone()
        return int(row["attempt_count"])

    def activate_server_ai_provider(self, provider: str, model: str) -> int:
        now = utc_now()
        cursor = self.connection.execute(
            """
            UPDATE server_ai_jobs
            SET status = 'queued', model = ?, next_attempt_at = ?, updated_at = ?
            WHERE provider = ? AND status = 'awaiting_key'
            """,
            (model.strip(), now, now, provider.strip().casefold()),
        )
        self.connection.commit()
        return int(cursor.rowcount)

    def mark_server_ai_job_awaiting_key(self, job_id: int) -> dict[str, Any]:
        now = utc_now()
        self.connection.execute(
            """
            UPDATE server_ai_jobs
            SET status = 'awaiting_key', started_at = NULL,
                next_attempt_at = NULL, updated_at = ?
            WHERE id = ? AND status = 'running'
            """,
            (now, job_id),
        )
        self.connection.commit()
        return self.get_server_ai_job(job_id)

    def recover_stale_server_ai_jobs(self, stale_before: str) -> int:
        now = utc_now()
        cursor = self.connection.execute(
            """
            UPDATE server_ai_jobs
            SET status = 'retrying', started_at = NULL,
                next_attempt_at = ?, updated_at = ?,
                last_error = CASE
                  WHEN last_error = '' THEN 'worker interrupted before completion'
                  ELSE last_error
                END
            WHERE status = 'running'
              AND started_at IS NOT NULL
              AND started_at < ?
            """,
            (now, now, stale_before),
        )
        self.connection.commit()
        return int(cursor.rowcount)

    def claim_next_server_ai_job(
        self, eligible_providers: set[str] | frozenset[str]
    ) -> dict[str, Any] | None:
        providers = sorted(
            {item.strip().casefold() for item in eligible_providers if item.strip()}
        )
        if not providers:
            return None
        placeholders = ",".join("?" for _ in providers)
        now = utc_now()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                f"""
                SELECT id
                FROM server_ai_jobs
                WHERE status IN ('queued', 'retrying')
                  AND provider IN ({placeholders})
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """,
                (*providers, now),
            ).fetchone()
            if row is None:
                self.connection.commit()
                return None
            cursor = self.connection.execute(
                """
                UPDATE server_ai_jobs
                SET status = 'running', attempts = attempts + 1,
                    started_at = ?, next_attempt_at = NULL, updated_at = ?
                WHERE id = ? AND status IN ('queued', 'retrying')
                """,
                (now, now, row["id"]),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                return None
            job_id = int(row["id"])
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return self.get_server_ai_job(job_id)

    def reschedule_server_ai_job(
        self, job_id: int, next_attempt_at: str, error: str
    ) -> dict[str, Any]:
        now = utc_now()
        self.connection.execute(
            """
            UPDATE server_ai_jobs
            SET status = 'retrying', started_at = NULL,
                next_attempt_at = ?, last_error = ?, updated_at = ?
            WHERE id = ? AND status = 'running'
            """,
            (next_attempt_at, error.strip()[:1000], now, job_id),
        )
        self.connection.commit()
        return self.get_server_ai_job(job_id)

    def list_server_ai_context_entries(
        self, source_entry_id: int, limit: int
    ) -> list[dict[str, Any]]:
        source = self.get_entry(source_entry_id)
        clean_limit = min(100, max(1, int(limit)))
        task_filter = "task_id IS NULL" if source.get("task_id") is None else "task_id = ?"
        params: tuple[Any, ...] = (
            (source["project_id"], source_entry_id, clean_limit)
            if source.get("task_id") is None
            else (source["project_id"], source["task_id"], source_entry_id, clean_limit)
        )
        rows = self.connection.execute(
            f"""
            SELECT *
            FROM entries
            WHERE project_id = ?
              AND {task_filter}
              AND id <= ?
              AND deleted_at IS NULL
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def finish_server_ai_job(
        self,
        job_id: int,
        body_markdown: str,
        metadata_json: str,
        usage_json: str = "{}",
        succeeded: bool = True,
        last_error: str = "",
    ) -> dict[str, Any]:
        body = validate_device_entry_text(body_markdown)
        now = utc_now()
        final_status = "succeeded" if succeeded else "failed"
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            job = self.connection.execute(
                """
                SELECT server_ai_jobs.*, entries.task_id AS source_task_id,
                       devices.label AS target_device_label
                FROM server_ai_jobs
                JOIN entries ON entries.id = server_ai_jobs.source_entry_id
                JOIN devices ON devices.id = server_ai_jobs.target_device_id
                WHERE server_ai_jobs.id = ?
                """,
                (job_id,),
            ).fetchone()
            if job is None:
                raise ValueError("server AI job not found")
            if job["response_entry_id"] is not None:
                response_entry_id = int(job["response_entry_id"])
                self.connection.commit()
                return self.get_entry(response_entry_id)
            response_metadata = json.loads(metadata_json or "{}")
            response_metadata["routing"] = {"mode": "none", "device_ids": []}
            response_metadata["reply_to_entry_id"] = int(job["source_entry_id"])
            metadata_json = json.dumps(response_metadata, ensure_ascii=False, separators=(",", ":"))
            cursor = self.connection.execute(
                """
                INSERT INTO entries
                  (project_id, author_type, author_device_id, source_label,
                   kind, body_markdown, metadata_json, task_id,
                   created_at, updated_at)
                VALUES (?, 'device', ?, ?, 'markdown', ?, ?, ?, ?, ?)
                """,
                (
                    job["project_id"],
                    job["target_device_id"],
                    job["target_device_label"],
                    body,
                    metadata_json,
                    job["source_task_id"],
                    now,
                    now,
                ),
            )
            response_entry_id = int(cursor.lastrowid)
            self.connection.execute(
                """
                UPDATE server_ai_jobs
                SET status = ?, response_entry_id = ?, finished_at = ?,
                    started_at = NULL, next_attempt_at = NULL,
                    last_error = ?, usage_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    final_status,
                    response_entry_id,
                    now,
                    last_error.strip()[:1000],
                    usage_json,
                    now,
                    job_id,
                ),
            )
            self.connection.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (now, job["project_id"]),
            )
            if not succeeded:
                NotificationStore(self.connection).capture_event(
                    event_type="ai.failed", entity_type="server_ai_job", entity_id=job_id,
                    status_version=1, project_id=job["project_id"], occurred_at=now,
                    deployment_enabled=self.email_notifications_enabled,
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return self.get_entry(response_entry_id)

    def build_task_prompt_markdown(self, project_slug: str, task_id: int) -> str:
        project = self.get_project_by_slug(project_slug)
        task = self.get_task(task_id)
        if not project or not task or task["project_id"] != project["id"]:
            raise ValueError("task not found")
        if task.get("assigned_agent_id"):
            agent = AgentStore(self.connection).get(project["id"], task["assigned_agent_id"])
            task = dict(task, assigned_agent_kind=agent["kind"])
        entries = self.list_entries(project["id"], task_id=task_id)
        return render_task_prompt_markdown(
            project=project,
            control=self.get_latest_project_control(project["id"]),
            task=task,
            entries=entries,
            attachments=self.list_attachments_for_entries(
                project["id"], [int(entry["id"]) for entry in entries]
            ),
            primary_device=self.get_project_primary_device(project["id"]),
        ) + AgentStore(self.connection).prompt(task)

    def add_project_member(
        self,
        project_id: int,
        device_id: int,
        permission: str,
        added_by_user_id: int | None = None,
        added_by_device_id: int | None = None,
        commit: bool = True,
    ) -> None:
        if permission not in PERMISSION_RANK:
            raise ValueError("invalid permission")
        self.connection.execute(
            """
            INSERT INTO project_memberships
              (project_id, device_id, permission, added_by_user_id,
               added_by_device_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, device_id)
            DO UPDATE SET permission = excluded.permission
            """,
            (
                project_id,
                device_id,
                permission,
                added_by_user_id,
                added_by_device_id,
                utc_now(),
            ),
        )
        if commit:
            self.connection.commit()

    def remove_project_member(self, project_id: int, device_id: int) -> None:
        self.connection.execute(
            """
            UPDATE projects
            SET primary_device_id = NULL, updated_at = ?
            WHERE id = ? AND primary_device_id = ?
            """,
            (utc_now(), project_id, device_id),
        )
        self.connection.execute(
            """
            DELETE FROM project_chat_roles
            WHERE project_id = ? AND device_id = ?
            """,
            (project_id, device_id),
        )
        self.connection.execute(
            """
            DELETE FROM project_memberships
            WHERE project_id = ? AND device_id = ?
            """,
            (project_id, device_id),
        )
        self.connection.commit()

    def get_project_membership(
        self, project_id: int, device_id: int
    ) -> dict[str, Any] | None:
        return _row_to_dict(
            self.connection.execute(
                """
                SELECT project_memberships.*, devices.label AS device_label,
                       EXISTS(SELECT 1 FROM hub_agent_connections c WHERE c.device_id=devices.id) AS agent_tasks_only,
                       devices.server_ai_provider AS server_ai_provider,
                       devices.last_seen_at AS last_seen_at,
                       devices.last_web_seen_at AS last_web_seen_at,
                       devices.last_agent_seen_at AS last_agent_seen_at
                FROM project_memberships
                JOIN devices ON devices.id = project_memberships.device_id
                WHERE project_memberships.project_id = ?
                  AND project_memberships.device_id = ?
                """,
                (project_id, device_id),
            ).fetchone()
        )

    def list_project_memberships(self, project_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT project_memberships.*, devices.label AS device_label,
                       EXISTS(SELECT 1 FROM hub_agent_connections c WHERE c.device_id=devices.id) AS agent_tasks_only,
                       devices.server_ai_provider AS server_ai_provider,
                       devices.last_seen_at AS last_seen_at,
                       devices.last_web_seen_at AS last_web_seen_at,
                       devices.last_agent_seen_at AS last_agent_seen_at,
                       device_groups.name AS group_name
                FROM project_memberships
                JOIN devices ON devices.id = project_memberships.device_id
                LEFT JOIN device_groups ON device_groups.id = devices.group_id
                WHERE project_memberships.project_id = ?
                ORDER BY devices.label
                """,
                (project_id,),
            ).fetchall()
        ]

    def add_project_chat_role(
        self,
        project_id: int,
        name: str,
        device_id: int,
        instructions: str = "",
        added_by_user_id: int | None = None,
    ) -> dict[str, Any]:
        clean_name = name.strip().lstrip("@")
        if not CHAT_ROLE_NAME_RE.fullmatch(clean_name):
            raise ValueError(
                "role name must be 1-40 letters, numbers, Chinese characters, dots, dashes, or underscores"
            )
        if not self.can_device_write(project_id, device_id):
            raise ValueError("role device must be a writable project member")

        reserved_names: set[str] = set()
        for member in self.list_project_memberships(project_id):
            reserved_names.add(str(member["device_label"]).casefold())
            if member.get("group_name"):
                reserved_names.add(str(member["group_name"]).casefold())
        if clean_name.casefold() in reserved_names:
            raise ValueError("role name conflicts with a project device or device group")

        existing = next(
            (
                role
                for role in self.list_project_chat_roles(project_id)
                if str(role["name"]).casefold() == clean_name.casefold()
            ),
            None,
        )
        now = utc_now()
        if existing:
            self.connection.execute(
                """
                UPDATE project_chat_roles
                SET name = ?, instructions = ?, device_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (clean_name, instructions.strip(), device_id, now, existing["id"]),
            )
            role_id = int(existing["id"])
        else:
            cursor = self.connection.execute(
                """
                INSERT INTO project_chat_roles
                  (project_id, name, instructions, device_id,
                   created_by_user_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    clean_name,
                    instructions.strip(),
                    device_id,
                    added_by_user_id,
                    now,
                    now,
                ),
            )
            role_id = int(cursor.lastrowid)
        self.connection.commit()
        return self.get_project_chat_role(role_id)

    def get_project_chat_role(self, role_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT project_chat_roles.*, devices.label AS device_label
            FROM project_chat_roles
            JOIN devices ON devices.id = project_chat_roles.device_id
            WHERE project_chat_roles.id = ?
            """,
            (role_id,),
        ).fetchone()
        if row is None:
            raise ValueError("project chat role not found")
        return dict(row)

    def list_project_chat_roles(
        self, project_id: int, device_id: int | None = None
    ) -> list[dict[str, Any]]:
        device_filter = "" if device_id is None else "AND project_chat_roles.device_id = ?"
        params: tuple[int, ...] = (
            (project_id,) if device_id is None else (project_id, device_id)
        )
        return [
            dict(row)
            for row in self.connection.execute(
                f"""
                SELECT project_chat_roles.*, devices.label AS device_label
                FROM project_chat_roles
                JOIN devices ON devices.id = project_chat_roles.device_id
                WHERE project_chat_roles.project_id = ?
                {device_filter}
                ORDER BY project_chat_roles.name COLLATE NOCASE
                """,
                params,
            ).fetchall()
        ]

    def list_chat_role_names_for_device(self, device_id: int) -> list[str]:
        return [
            str(row["name"])
            for row in self.connection.execute(
                """
                SELECT project_chat_roles.name
                FROM project_chat_roles
                JOIN project_memberships
                  ON project_memberships.project_id = project_chat_roles.project_id
                 AND project_memberships.device_id = project_chat_roles.device_id
                WHERE project_chat_roles.device_id = ?
                ORDER BY project_chat_roles.name COLLATE NOCASE
                """,
                (device_id,),
            ).fetchall()
        ]

    def remove_project_chat_role(self, project_id: int, role_id: int) -> None:
        self.connection.execute(
            "DELETE FROM project_chat_roles WHERE project_id = ? AND id = ?",
            (project_id, role_id),
        )
        self.connection.commit()

    def can_device_read(self, project_id: int, device_id: int) -> bool:
        return self._device_has_permission(project_id, device_id, "read")

    def can_device_write(self, project_id: int, device_id: int) -> bool:
        return self._device_has_permission(project_id, device_id, "write")

    def _device_has_permission(
        self, project_id: int, device_id: int, required: str
    ) -> bool:
        membership = self.get_project_membership(project_id, device_id)
        if not membership:
            return False
        return PERMISSION_RANK[membership["permission"]] >= PERMISSION_RANK[required]

    def create_codex_workspace(
        self,
        slug: str,
        name: str,
        description: str = "",
        created_by_user_id: int | None = None,
    ) -> dict[str, Any]:
        clean_slug = slug.strip().lower()
        clean_name = name.strip() or clean_slug
        now = utc_now()
        existing = self.get_codex_workspace_by_slug(clean_slug, include_deleted=True)
        if existing and not existing.get("deleted_at"):
            raise ValueError("codex workspace already exists")
        if existing:
            self.connection.execute(
                """
                UPDATE codex_workspaces
                SET name = ?,
                    description = ?,
                    deleted_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (clean_name, description.strip(), now, existing["id"]),
            )
            self.connection.commit()
            return self.get_codex_workspace(existing["id"])
        cursor = self.connection.execute(
            """
            INSERT INTO codex_workspaces
              (slug, name, description, created_by_user_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                clean_slug,
                clean_name,
                description.strip(),
                created_by_user_id,
                now,
                now,
            ),
        )
        self.connection.commit()
        return self.get_codex_workspace(cursor.lastrowid)

    def get_codex_workspace(
        self, workspace_id: int, include_deleted: bool = False
    ) -> dict[str, Any] | None:
        deleted_filter = "" if include_deleted else "AND deleted_at IS NULL"
        return _row_to_dict(
            self.connection.execute(
                f"""
                SELECT *
                FROM codex_workspaces
                WHERE id = ?
                {deleted_filter}
                """,
                (workspace_id,),
            ).fetchone()
        )

    def get_codex_workspace_by_slug(
        self, slug: str, include_deleted: bool = False
    ) -> dict[str, Any] | None:
        deleted_filter = "" if include_deleted else "AND deleted_at IS NULL"
        return _row_to_dict(
            self.connection.execute(
                f"""
                SELECT *
                FROM codex_workspaces
                WHERE slug = ?
                {deleted_filter}
                """,
                (slug.strip().lower(),),
            ).fetchone()
        )

    def list_codex_workspaces(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT codex_workspaces.*,
                       COUNT(codex_conversations.id) AS conversation_count,
                       MAX(codex_conversations.created_at) AS latest_conversation_at
                FROM codex_workspaces
                LEFT JOIN codex_conversations
                  ON codex_conversations.workspace_id = codex_workspaces.id
                WHERE codex_workspaces.deleted_at IS NULL
                GROUP BY codex_workspaces.id
                ORDER BY COALESCE(latest_conversation_at, codex_workspaces.updated_at) DESC,
                         codex_workspaces.name COLLATE NOCASE ASC
                """
            ).fetchall()
        ]

    def delete_codex_workspace(self, workspace_id: int) -> dict[str, Any] | None:
        workspace = self.get_codex_workspace(workspace_id)
        if not workspace:
            return None
        now = utc_now()
        self.connection.execute(
            """
            UPDATE codex_workspaces
            SET deleted_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (now, now, workspace_id),
        )
        self.connection.commit()
        return self.get_codex_workspace(workspace_id, include_deleted=True)

    def create_codex_conversation(
        self,
        workspace_id: int,
        hub_project_id: int,
        title: str,
        initial_prompt: str,
        target_device_id: int | None = None,
        task_id: int | None = None,
        status: str = "requested",
        created_by_user_id: int | None = None,
    ) -> dict[str, Any]:
        if not self.get_codex_workspace(workspace_id):
            raise ValueError("codex workspace not found")
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("conversation title is required")
        now = utc_now()
        cursor = self.connection.execute(
            """
            INSERT INTO codex_conversations
              (workspace_id, hub_project_id, task_id, title, initial_prompt,
               target_device_id, status, created_by_user_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace_id,
                hub_project_id,
                task_id,
                clean_title,
                initial_prompt.strip(),
                target_device_id,
                status.strip() or "requested",
                created_by_user_id,
                now,
                now,
            ),
        )
        self.connection.execute(
            "UPDATE codex_workspaces SET updated_at = ? WHERE id = ?",
            (now, workspace_id),
        )
        self.connection.commit()
        return self.get_codex_conversation(cursor.lastrowid)

    def get_codex_conversation(
        self, conversation_id: int
    ) -> dict[str, Any] | None:
        return _row_to_dict(
            self.connection.execute(
                """
                SELECT codex_conversations.*,
                       codex_workspaces.slug AS workspace_slug,
                       codex_workspaces.name AS workspace_name,
                       devices.label AS target_device_label,
                       tasks.status AS task_status
                FROM codex_conversations
                JOIN codex_workspaces
                  ON codex_workspaces.id = codex_conversations.workspace_id
                LEFT JOIN devices
                  ON devices.id = codex_conversations.target_device_id
                LEFT JOIN tasks
                  ON tasks.id = codex_conversations.task_id
                WHERE codex_conversations.id = ?
                """,
                (conversation_id,),
            ).fetchone()
        )

    def list_codex_conversations(
        self,
        hub_project_id: int | None = None,
        workspace_id: int | None = None,
        limit: int = 60,
    ) -> list[dict[str, Any]]:
        filters: list[str] = ["codex_workspaces.deleted_at IS NULL"]
        params: list[Any] = []
        if hub_project_id is not None:
            filters.append("codex_conversations.hub_project_id = ?")
            params.append(hub_project_id)
        if workspace_id is not None:
            filters.append("codex_conversations.workspace_id = ?")
            params.append(workspace_id)
        params.append(limit)
        return [
            dict(row)
            for row in self.connection.execute(
                f"""
                SELECT codex_conversations.*,
                       codex_workspaces.slug AS workspace_slug,
                       codex_workspaces.name AS workspace_name,
                       devices.label AS target_device_label,
                       tasks.status AS task_status
                FROM codex_conversations
                JOIN codex_workspaces
                  ON codex_workspaces.id = codex_conversations.workspace_id
                LEFT JOIN devices
                  ON devices.id = codex_conversations.target_device_id
                LEFT JOIN tasks
                  ON tasks.id = codex_conversations.task_id
                WHERE {" AND ".join(filters)}
                ORDER BY codex_conversations.created_at DESC,
                         codex_conversations.id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        ]

    def attach_codex_conversation_task(
        self, conversation_id: int, task_id: int, status: str = "task_created"
    ) -> dict[str, Any]:
        now = utc_now()
        self.connection.execute(
            """
            UPDATE codex_conversations
            SET task_id = ?,
                status = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (task_id, status, now, conversation_id),
        )
        self.connection.commit()
        return self.get_codex_conversation(conversation_id)

    def _freeze_entry_routing(
        self,
        *,
        project_id: int,
        body_markdown: str,
        author_user_id: int | None,
        author_device_id: int | None,
        task_id: int | None,
        metadata_json: str,
    ) -> str:
        """Resolve recipients once, independently of mutable project preferences."""
        try:
            metadata = json.loads(metadata_json or "{}")
        except (TypeError, ValueError) as exc:
            raise ValueError("message metadata must be a JSON object") from exc
        if not isinstance(metadata, dict):
            raise ValueError("message metadata must be a JSON object")
        project = self.get_project(project_id)
        task = self.get_task(task_id) if task_id is not None else None
        if task_id is not None and (not task or task["project_id"] != project_id):
            raise ValueError("task does not belong to this project")
        if task and task.get("assigned_agent_id"):
            # Work-room messages are task evidence, not another device dispatch.
            # Explicit continuation creates a new agent task through /dispatch.
            metadata["routing"] = {"mode": "none", "device_ids": []}
            return json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        members = {
            int(row["id"]): dict(row)
            for row in self.connection.execute(
                """
                SELECT devices.*, project_memberships.permission,
                       device_groups.name AS group_name
                FROM project_memberships
                JOIN devices ON devices.id = project_memberships.device_id
                LEFT JOIN device_groups ON device_groups.id = devices.group_id
                WHERE project_memberships.project_id = ? AND devices.is_active = 1
                  AND NOT EXISTS(SELECT 1 FROM hub_agent_connections c WHERE c.device_id=devices.id)
                """, (project_id,)
            ).fetchall()
        }
        writable_ids = {
            device_id for device_id, member in members.items()
            if member.get("permission") in {"write", "admin"}
        }
        routing = metadata.get("routing")
        if "reply_to_entry_id" in metadata:
            reply_id = metadata["reply_to_entry_id"]
            if isinstance(reply_id, bool) or not isinstance(reply_id, int) or reply_id <= 0:
                raise ValueError("reply_to_entry_id must be a positive entry ID")
            source_row = self.connection.execute(
                "SELECT * FROM entries WHERE id = ? AND deleted_at IS NULL", (reply_id,)
            ).fetchone()
            source = dict(source_row) if source_row is not None else None
            if not source or source["project_id"] != project_id or source.get("task_id") != task_id:
                raise ValueError("reply source must belong to this conversation")
            if author_device_id is not None:
                source_routing = entry_routing(source) or {}
                source_ids = source_routing.get("device_ids", [])
                if source_routing and author_device_id not in source_ids:
                    raise ValueError("replying device was not selected for this message")
                # A group advisor may notify its already-selected primary once.
                # Direct answers and replies to that summary never fan out again.
                primary_id = (project or {}).get("primary_device_id")
                if (
                    source.get("author_type") == "user"
                    and source_routing.get("mode") == "group"
                    and primary_id in source_ids
                    and primary_id != author_device_id
                    and primary_id in writable_ids
                ):
                    routing = {"mode": "selected", "device_ids": [primary_id]}
                else:
                    routing = {"mode": "none", "device_ids": []}
        if routing is not None:
            if not isinstance(routing, dict):
                raise ValueError("routing must be an object")
            mode = str(routing.get("mode") or "selected").strip().lower()
            if mode not in {"selected", "group", "none"}:
                raise ValueError("delivery mode must be selected, group, or none")
            requested_ids = routing.get("device_ids", [])
            if not isinstance(requested_ids, list) or any(
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
                for value in requested_ids
            ):
                raise ValueError("target device IDs must be a list of positive integers")
            if mode == "group":
                target_ids = sorted(writable_ids - {author_device_id})
            elif mode == "none":
                target_ids = []
            else:
                target_ids = list(dict.fromkeys(requested_ids))
                if any(device_id not in writable_ids for device_id in target_ids):
                    raise ValueError("selected devices must be active writable project members")
                if not target_ids:
                    raise ValueError("select at least one member or use group mode")
        else:
            tokens = extract_mention_tokens(body_markdown)
            role_names: dict[int, list[str]] = {}
            for role in self.list_project_chat_roles(project_id):
                role_names.setdefault(int(role["device_id"]), []).append(role["name"])
            target_ids = [
                device_id for device_id, member in members.items()
                if device_id != author_device_id and _mention_target_sets(
                    body_markdown, member, role_names.get(device_id, [])
                )[1]
            ]
            # Older API clients can still explicitly @ a device, group, or role.
            # Unaddressed human messages default to one task owner / primary.
            if not tokens and author_user_id is not None:
                preferred_id = (
                    (task or {}).get("claimed_by_device_id")
                    or (task or {}).get("assigned_device_id")
                    or (project or {}).get("primary_device_id")
                )
                if preferred_id in writable_ids:
                    target_ids = [preferred_id]
                elif len(writable_ids) == 1:
                    target_ids = sorted(writable_ids)
            mode = "selected" if target_ids else "none"
        metadata["routing"] = {"mode": mode, "device_ids": target_ids}
        return json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))

    def create_entry(
        self,
        project_id: int,
        source_label: str,
        kind: str,
        body_markdown: str,
        author_user_id: int | None = None,
        author_device_id: int | None = None,
        task_id: int | None = None,
        metadata_json: str = "{}",
        *,
        commit: bool = True,
        execution_fenced: bool = False,
    ) -> dict[str, Any]:
        if task_id is not None and not execution_fenced:
            with NotificationStore(self.connection).transaction():
                guard_legacy_task_write(self.connection, self.get_task(task_id), author_device_id)
                return self.create_entry(project_id, source_label, kind, body_markdown,
                    author_user_id=author_user_id, author_device_id=author_device_id,
                    task_id=task_id, metadata_json=metadata_json, commit=False, execution_fenced=True)
        if author_device_id is not None:
            body_markdown = validate_device_entry_text(body_markdown)
        metadata_json = self._freeze_entry_routing(
            project_id=project_id, body_markdown=body_markdown,
            author_user_id=author_user_id, author_device_id=author_device_id,
            task_id=task_id, metadata_json=metadata_json,
        )
        now = utc_now()
        author_type = "user" if author_user_id is not None else "device"
        cursor = self.connection.execute(
            """
            INSERT INTO entries
              (project_id, author_type, author_user_id, author_device_id,
               source_label, kind, body_markdown, metadata_json, task_id,
               created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                author_type,
                author_user_id,
                author_device_id,
                source_label,
                kind,
                body_markdown,
                metadata_json,
                task_id,
                now,
                now,
            ),
        )
        self.connection.execute(
            "UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id)
        )
        if commit:
            self.connection.commit()
        return self.get_entry(cursor.lastrowid)

    def create_entry_with_attachments(
        self,
        *,
        project_id: int,
        source_label: str,
        kind: str,
        body_markdown: str,
        attachments: list[dict[str, Any]],
        author_user_id: int | None = None,
        author_device_id: int | None = None,
        task_id: int | None = None,
        metadata_json: str = "{}",
        commit: bool = True,
        execution_fenced: bool = False,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Create one chat message and all of its attachments atomically."""

        if task_id is not None and not execution_fenced:
            with NotificationStore(self.connection).transaction():
                guard_legacy_task_write(self.connection, self.get_task(task_id), author_device_id)
                return self.create_entry_with_attachments(project_id=project_id,
                    source_label=source_label, kind=kind, body_markdown=body_markdown, attachments=attachments,
                    author_user_id=author_user_id, author_device_id=author_device_id, task_id=task_id,
                    metadata_json=metadata_json, commit=False, execution_fenced=True)

        if author_device_id is not None:
            body_markdown = validate_device_entry_text(body_markdown)
        metadata_json = self._freeze_entry_routing(
            project_id=project_id, body_markdown=body_markdown,
            author_user_id=author_user_id, author_device_id=author_device_id,
            task_id=task_id, metadata_json=metadata_json,
        )
        now = utc_now()
        author_type = "user" if author_user_id is not None else "device"
        attachment_ids: list[int] = []
        try:
            entry_cursor = self.connection.execute(
                """
                INSERT INTO entries
                  (project_id, author_type, author_user_id, author_device_id,
                   source_label, kind, body_markdown, metadata_json, task_id,
                   created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    author_type,
                    author_user_id,
                    author_device_id,
                    source_label,
                    kind,
                    body_markdown,
                    metadata_json,
                    task_id,
                    now,
                    now,
                ),
            )
            for attachment in attachments:
                attachment_cursor = self.connection.execute(
                    """
                    INSERT INTO attachments
                      (project_id, entry_id, original_filename, stored_relpath,
                       content_type, byte_size, sha256, created_by_user_id,
                       created_by_device_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        entry_cursor.lastrowid,
                        attachment["original_filename"],
                        attachment["stored_relpath"],
                        attachment["content_type"],
                        attachment["byte_size"],
                        attachment["sha256"],
                        author_user_id,
                        author_device_id,
                        now,
                    ),
                )
                attachment_ids.append(int(attachment_cursor.lastrowid))
            self.connection.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id)
            )
            if commit:
                self.connection.commit()
        except Exception:
            if commit:
                self.connection.rollback()
            raise
        return (
            self.get_entry(entry_cursor.lastrowid),
            [self.get_attachment(attachment_id) for attachment_id in attachment_ids],
        )

    def get_entry(self, entry_id: int) -> dict[str, Any]:
        return dict(
            self.connection.execute(
                "SELECT * FROM entries WHERE id = ?", (entry_id,)
            ).fetchone()
        )

    def list_entries(
        self, project_id: int, task_id: int
    ) -> list[dict[str, Any]]:
        """Return one task thread. Project and all-entry scopes are explicit."""

        return [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT *
                FROM entries
                WHERE project_id = ?
                  AND task_id = ?
                  AND deleted_at IS NULL
                ORDER BY created_at ASC, id ASC
                """,
                (project_id, int(task_id)),
            ).fetchall()
        ]

    def list_project_entries(self, project_id: int) -> list[dict[str, Any]]:
        """Return only project-level chat entries, excluding task threads."""

        return [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT *
                FROM entries
                WHERE project_id = ?
                  AND task_id IS NULL
                  AND deleted_at IS NULL
                ORDER BY created_at ASC, id ASC
                """,
                (project_id,),
            ).fetchall()
        ]

    def list_all_entries(self, project_id: int) -> list[dict[str, Any]]:
        """Return the explicit project-wide archive, including task threads."""

        return [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT *
                FROM entries
                WHERE project_id = ? AND deleted_at IS NULL
                ORDER BY created_at ASC, id ASC
                """,
                (project_id,),
            ).fetchall()
        ]

    def list_entries_after(
        self,
        project_id: int,
        after_entry_id: int = 0,
        limit: int = 50,
    ) -> tuple[int, int, bool, list[dict[str, Any]]]:
        """Return a bounded, lossless page for the live project chat."""

        clean_after = max(0, int(after_entry_id))
        clean_limit = min(100, max(1, int(limit)))
        latest_row = self.connection.execute(
            """
            SELECT COALESCE(MAX(id), 0) AS latest_entry_id
            FROM entries
            WHERE project_id = ?
              AND task_id IS NULL
              AND deleted_at IS NULL
            """,
            (project_id,),
        ).fetchone()
        latest_entry_id = int(latest_row["latest_entry_id"])
        rows = self.connection.execute(
            """
            SELECT *
            FROM entries
            WHERE project_id = ?
              AND id > ?
              AND task_id IS NULL
              AND deleted_at IS NULL
            ORDER BY id ASC
            LIMIT ?
            """,
            (project_id, clean_after, clean_limit + 1),
        ).fetchall()
        has_more = len(rows) > clean_limit
        entries = [dict(row) for row in rows[:clean_limit]]
        next_entry_id = (
            int(entries[-1]["id"])
            if has_more and entries
            else latest_entry_id
        )
        return latest_entry_id, next_entry_id, has_more, entries

    def list_mobile_notifications(
        self, after_entry_id: int = 0, limit: int = 50
    ) -> tuple[int, list[dict[str, Any]]]:
        """Return the next page of device replies and a lossless cursor."""

        clean_after = max(0, int(after_entry_id))
        clean_limit = min(100, max(1, int(limit)))
        latest_row = self.connection.execute(
            """
            SELECT COALESCE(MAX(id), 0) AS latest_entry_id
            FROM entries
            WHERE deleted_at IS NULL
            """
        ).fetchone()
        rows = self.connection.execute(
            """
            SELECT entries.id,
                   entries.project_id,
                   entries.source_label,
                   entries.kind,
                   entries.body_markdown,
                   entries.created_at,
                   entries.task_id,
                   projects.slug AS project_slug,
                   projects.name AS project_name
            FROM entries
            JOIN projects ON projects.id = entries.project_id
            WHERE entries.id > ?
              AND entries.deleted_at IS NULL
              AND entries.author_type = 'device'
            ORDER BY entries.id ASC
            LIMIT ?
            """,
            (clean_after, clean_limit + 1),
        ).fetchall()
        has_more = len(rows) > clean_limit
        notifications = [dict(row) for row in rows[:clean_limit]]
        if has_more:
            next_entry_id = int(notifications[-1]["id"])
        else:
            next_entry_id = int(latest_row["latest_entry_id"])
        return next_entry_id, notifications

    def create_attachment(
        self,
        project_id: int,
        original_filename: str,
        stored_relpath: str,
        content_type: str,
        byte_size: int,
        sha256: str,
        entry_id: int | None = None,
        created_by_user_id: int | None = None,
        created_by_device_id: int | None = None,
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            """
            INSERT INTO attachments
              (project_id, entry_id, original_filename, stored_relpath, content_type,
               byte_size, sha256, created_by_user_id, created_by_device_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                entry_id,
                original_filename,
                stored_relpath,
                content_type,
                byte_size,
                sha256,
                created_by_user_id,
                created_by_device_id,
                utc_now(),
            ),
        )
        self.connection.commit()
        return self.get_attachment(cursor.lastrowid)

    def create_attachment_entry(
        self,
        *,
        project_id: int,
        source_label: str,
        original_filename: str,
        stored_relpath: str,
        content_type: str,
        byte_size: int,
        sha256: str,
        created_by_user_id: int | None = None,
        created_by_device_id: int | None = None,
        task_id: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Create the file message and its attachment in one transaction."""

        now = utc_now()
        author_type = "user" if created_by_user_id is not None else "device"
        body_markdown = f"Uploaded attachment: {original_filename}"
        if created_by_device_id is not None:
            body_markdown = validate_device_entry_text(body_markdown)
        try:
            entry_cursor = self.connection.execute(
                """
                INSERT INTO entries
                  (project_id, author_type, author_user_id, author_device_id,
                   source_label, kind, body_markdown, metadata_json, task_id,
                   created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'file', ?, '{}', ?, ?, ?)
                """,
                (
                    project_id,
                    author_type,
                    created_by_user_id,
                    created_by_device_id,
                    source_label,
                    body_markdown,
                    task_id,
                    now,
                    now,
                ),
            )
            attachment_cursor = self.connection.execute(
                """
                INSERT INTO attachments
                  (project_id, entry_id, original_filename, stored_relpath,
                   content_type, byte_size, sha256, created_by_user_id,
                   created_by_device_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    entry_cursor.lastrowid,
                    original_filename,
                    stored_relpath,
                    content_type,
                    byte_size,
                    sha256,
                    created_by_user_id,
                    created_by_device_id,
                    now,
                ),
            )
            self.connection.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id)
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return (
            self.get_entry(entry_cursor.lastrowid),
            self.get_attachment(attachment_cursor.lastrowid),
        )

    def get_attachment(self, attachment_id: int) -> dict[str, Any] | None:
        return _row_to_dict(
            self.connection.execute(
                "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
            ).fetchone()
        )

    def list_attachments(self, project_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT *
                FROM attachments
                WHERE project_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (project_id,),
            ).fetchall()
        ]

    def list_attachments_for_entries(
        self, project_id: int, entry_ids: list[int]
    ) -> list[dict[str, Any]]:
        clean_ids = sorted({int(entry_id) for entry_id in entry_ids if int(entry_id) > 0})
        if not clean_ids:
            return []
        placeholders = ",".join("?" for _ in clean_ids)
        return [
            dict(row)
            for row in self.connection.execute(
                f"""
                SELECT *
                FROM attachments
                WHERE project_id = ? AND entry_id IN ({placeholders})
                ORDER BY created_at ASC, id ASC
                """,
                (project_id, *clean_ids),
            ).fetchall()
        ]

    def list_task_attachments(self, project_id: int, task_id: int) -> list[dict[str, Any]]:
        """Only files explicitly attached to this task, including traced delegation."""
        return [dict(row) for row in self.connection.execute('''
            SELECT a.id,a.entry_id,a.original_filename,a.content_type,a.byte_size,a.sha256,
                   ta.task_id,ta.source_task_id
            FROM hub_task_attachments ta JOIN attachments a ON a.id=ta.attachment_id
            JOIN tasks t ON t.id=ta.task_id
            WHERE t.id=? AND t.project_id=? AND a.project_id=t.project_id
            ORDER BY a.id''', (task_id, project_id))]

    def bind_task_attachments(self, project_id: int, task_ids: list[int], attachment_ids: list[int],
                              *, source_task_id: int | None = None) -> None:
        """Participate in the caller's dispatch transaction; never commit independently."""
        if not self.connection.in_transaction:
            raise ValueError('task attachments require an active transaction')
        for task_id in task_ids:
            task = self.get_task(task_id)
            if not task or task['project_id'] != project_id:
                raise ValueError('附件任务不属于当前项目')
            for attachment_id in attachment_ids:
                attachment = self.get_attachment(attachment_id)
                if not attachment or attachment['project_id'] != project_id:
                    raise ValueError('附件不属于当前项目')
                if source_task_id is not None and not self.connection.execute('''
                    SELECT 1 FROM hub_task_attachments ta JOIN tasks t ON t.id=ta.task_id
                    WHERE ta.task_id=? AND ta.attachment_id=? AND t.project_id=?''',
                    (source_task_id, attachment_id, project_id)).fetchone():
                    raise ValueError('委派附件不属于源任务')
                self.connection.execute('''INSERT INTO hub_task_attachments(task_id,attachment_id,source_task_id)
                    VALUES (?,?,?)''', (task_id, attachment_id, source_task_id))
