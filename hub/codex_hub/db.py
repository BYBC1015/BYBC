from __future__ import annotations

import sqlite3
from pathlib import Path

from .media_index import initialize_media_indexes
from .notifications.schema import initialize_notification_schema
from .mail.schema import initialize_mail_schema
from .agents import initialize_agent_schema


def _ensure_column(
    connection: sqlite3.Connection, table: str, column: str, ddl: str
) -> None:
    columns = {
        row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
    }
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def connect_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          username TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT 'owner',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          session_hash TEXT NOT NULL UNIQUE,
          csrf_token_hash TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS device_groups (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL UNIQUE,
          description TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS devices (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          label TEXT NOT NULL UNIQUE,
          group_id INTEGER REFERENCES device_groups(id) ON DELETE SET NULL,
          note TEXT NOT NULL DEFAULT '',
          server_ai_provider TEXT NOT NULL DEFAULT '',
          is_active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          last_seen_at TEXT,
          last_web_seen_at TEXT,
          last_agent_seen_at TEXT
        );

        CREATE TABLE IF NOT EXISTS api_tokens (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
          token_prefix TEXT NOT NULL,
          token_hash TEXT NOT NULL UNIQUE,
          name TEXT NOT NULL,
          is_active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          last_used_at TEXT,
          revoked_at TEXT
        );

        CREATE TABLE IF NOT EXISTS projects (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slug TEXT NOT NULL UNIQUE,
          name TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          primary_device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
          collaboration_enabled INTEGER NOT NULL DEFAULT 0,
          collaboration_started_at TEXT,
          collaboration_start_entry_id INTEGER NOT NULL DEFAULT 0,
          created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
          created_by_device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
          status TEXT NOT NULL DEFAULT 'active',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          archived_at TEXT
        );

        CREATE TABLE IF NOT EXISTS project_memberships (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
          permission TEXT NOT NULL,
          added_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
          added_by_device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          UNIQUE(project_id, device_id)
        );

        CREATE TABLE IF NOT EXISTS project_chat_roles (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          name TEXT NOT NULL,
          instructions TEXT NOT NULL DEFAULT '',
          device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
          created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(project_id, name)
        );

        CREATE TABLE IF NOT EXISTS entries (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          author_type TEXT NOT NULL,
          author_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
          author_device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
          source_label TEXT NOT NULL,
          kind TEXT NOT NULL,
          body_markdown TEXT NOT NULL DEFAULT '',
          metadata_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          deleted_at TEXT
        );

        CREATE TABLE IF NOT EXISTS attachments (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          entry_id INTEGER REFERENCES entries(id) ON DELETE SET NULL,
          original_filename TEXT NOT NULL,
          stored_relpath TEXT NOT NULL UNIQUE,
          content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
          byte_size INTEGER NOT NULL,
          sha256 TEXT NOT NULL,
          created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
          created_by_device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS audit_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          actor_type TEXT NOT NULL,
          actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
          actor_device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
          action TEXT NOT NULL,
          target_type TEXT NOT NULL,
          target_id INTEGER,
          metadata_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS project_control_documents (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          version INTEGER NOT NULL,
          goal TEXT NOT NULL DEFAULT '',
          non_goals TEXT NOT NULL DEFAULT '',
          constraints_text TEXT NOT NULL DEFAULT '',
          current_phase TEXT NOT NULL DEFAULT '',
          acceptance TEXT NOT NULL DEFAULT '',
          roles TEXT NOT NULL DEFAULT '',
          decisions TEXT NOT NULL DEFAULT '',
          risks TEXT NOT NULL DEFAULT '',
          architecture TEXT NOT NULL DEFAULT '',
          known_issues TEXT NOT NULL DEFAULT '',
          user_preferences TEXT NOT NULL DEFAULT '',
          key_files TEXT NOT NULL DEFAULT '',
          next_steps TEXT NOT NULL DEFAULT '',
          handoff_summary TEXT NOT NULL DEFAULT '',
          created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
          created_by_device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          UNIQUE(project_id, version)
        );

        CREATE TABLE IF NOT EXISTS project_handoffs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          version INTEGER NOT NULL,
          title TEXT NOT NULL DEFAULT '',
          summary_markdown TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'ready',
          base_memory_version INTEGER NOT NULL DEFAULT 0,
          source_device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
          target_device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
          manifest_json TEXT NOT NULL DEFAULT '{}',
          transfer_bytes INTEGER NOT NULL DEFAULT 0,
          large_file_count INTEGER NOT NULL DEFAULT 0,
          created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
          created_by_device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
          accepted_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
          accepted_by_device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          accepted_at TEXT,
          UNIQUE(project_id, version)
        );

        CREATE TABLE IF NOT EXISTS project_sync_snapshots (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
          revision INTEGER NOT NULL,
          memory_version INTEGER NOT NULL DEFAULT 0,
          git_commit TEXT NOT NULL DEFAULT '',
          manifest_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          UNIQUE(project_id, device_id, revision)
        );

        CREATE TABLE IF NOT EXISTS tasks (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          title TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'open',
          priority TEXT NOT NULL DEFAULT 'normal',
          assigned_device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
          claimed_by_device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
          requested_group_id INTEGER REFERENCES device_groups(id) ON DELETE SET NULL,
          created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
          created_by_device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
          context_pack_markdown TEXT NOT NULL DEFAULT '',
          result_summary TEXT NOT NULL DEFAULT '',
          due_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS mention_receipts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
          device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
          status TEXT NOT NULL DEFAULT 'claimed',
          delivery_id TEXT,
          claimed_at TEXT,
          dispatching_at TEXT,
          submitted_at TEXT,
          handled_at TEXT,
          last_error TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(entry_id, device_id)
        );

        CREATE TABLE IF NOT EXISTS server_ai_jobs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          source_entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
          target_device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
          provider TEXT NOT NULL,
          model TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'queued',
          attempts INTEGER NOT NULL DEFAULT 0,
          response_entry_id INTEGER REFERENCES entries(id) ON DELETE SET NULL,
          next_attempt_at TEXT,
          started_at TEXT,
          finished_at TEXT,
          last_error TEXT NOT NULL DEFAULT '',
          usage_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(source_entry_id, target_device_id)
        );

        CREATE TABLE IF NOT EXISTS codex_workspaces (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slug TEXT NOT NULL UNIQUE,
          name TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          deleted_at TEXT
        );

        CREATE TABLE IF NOT EXISTS codex_conversations (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          workspace_id INTEGER NOT NULL REFERENCES codex_workspaces(id) ON DELETE CASCADE,
          hub_project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
          task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
          title TEXT NOT NULL,
          initial_prompt TEXT NOT NULL DEFAULT '',
          target_device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
          status TEXT NOT NULL DEFAULT 'requested',
          created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          launched_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_entries_project_created
          ON entries(project_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_entries_project_id
          ON entries(project_id, id);
        CREATE INDEX IF NOT EXISTS idx_attachments_project
          ON attachments(project_id);
        CREATE INDEX IF NOT EXISTS idx_attachments_project_entry
          ON attachments(project_id, entry_id);
        CREATE INDEX IF NOT EXISTS idx_memberships_project_device
          ON project_memberships(project_id, device_id);
        CREATE INDEX IF NOT EXISTS idx_project_chat_roles_project_device
          ON project_chat_roles(project_id, device_id);
        CREATE INDEX IF NOT EXISTS idx_project_control_latest
          ON project_control_documents(project_id, version);
        CREATE INDEX IF NOT EXISTS idx_project_handoffs_latest
          ON project_handoffs(project_id, version);
        CREATE INDEX IF NOT EXISTS idx_project_sync_latest
          ON project_sync_snapshots(project_id, device_id, revision);
        CREATE INDEX IF NOT EXISTS idx_tasks_project_status
          ON tasks(project_id, status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_tasks_assigned
          ON tasks(assigned_device_id, status);
        CREATE INDEX IF NOT EXISTS idx_tasks_claimed
          ON tasks(claimed_by_device_id, status);
        CREATE INDEX IF NOT EXISTS idx_mention_receipts_device_status
          ON mention_receipts(device_id, status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_mention_receipts_entry_device
          ON mention_receipts(entry_id, device_id);
        CREATE INDEX IF NOT EXISTS idx_server_ai_jobs_claim
          ON server_ai_jobs(status, next_attempt_at, created_at);
        CREATE INDEX IF NOT EXISTS idx_server_ai_jobs_project
          ON server_ai_jobs(project_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_codex_workspaces_active
          ON codex_workspaces(deleted_at, updated_at);
        CREATE INDEX IF NOT EXISTS idx_codex_conversations_project_workspace
          ON codex_conversations(hub_project_id, workspace_id, created_at);
        """
    )
    _ensure_column(
        connection,
        "project_control_documents",
        "architecture",
        "architecture TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        connection,
        "project_control_documents",
        "known_issues",
        "known_issues TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        connection,
        "project_control_documents",
        "user_preferences",
        "user_preferences TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        connection,
        "project_control_documents",
        "key_files",
        "key_files TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        connection,
        "project_control_documents",
        "next_steps",
        "next_steps TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        connection,
        "projects",
        "primary_device_id",
        "primary_device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL",
    )
    _ensure_column(
        connection,
        "projects",
        "collaboration_enabled",
        "collaboration_enabled INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        connection,
        "projects",
        "collaboration_started_at",
        "collaboration_started_at TEXT",
    )
    _ensure_column(
        connection,
        "projects",
        "collaboration_start_entry_id",
        "collaboration_start_entry_id INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        connection,
        "entries",
        "task_id",
        "task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL",
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_entries_project_task_id "
        "ON entries(project_id, task_id, id)"
    )
    _ensure_column(
        connection,
        "devices",
        "server_ai_provider",
        "server_ai_provider TEXT NOT NULL DEFAULT ''",
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_server_ai_provider "
        "ON devices(server_ai_provider) WHERE server_ai_provider != ''"
    )
    _ensure_column(
        connection,
        "devices",
        "last_web_seen_at",
        "last_web_seen_at TEXT",
    )
    _ensure_column(
        connection,
        "devices",
        "last_agent_seen_at",
        "last_agent_seen_at TEXT",
    )
    _ensure_column(
        connection,
        "mention_receipts",
        "delivery_id",
        "delivery_id TEXT",
    )
    _ensure_column(
        connection,
        "mention_receipts",
        "dispatching_at",
        "dispatching_at TEXT",
    )
    _ensure_column(
        connection,
        "mention_receipts",
        "submitted_at",
        "submitted_at TEXT",
    )
    _ensure_column(
        connection,
        "mention_receipts",
        "last_error",
        "last_error TEXT NOT NULL DEFAULT ''",
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_entries_task_created
          ON entries(task_id, created_at)
        """
    )
    connection.commit()
    initialize_media_indexes(connection)
    initialize_notification_schema(connection)
    initialize_mail_schema(connection)
    initialize_agent_schema(connection)
    from .execution_leases import initialize_execution_schema
    initialize_execution_schema(connection)
    from .model_policy import initialize_model_policy_schema
    initialize_model_policy_schema(connection)
    from .maintenance import initialize_maintenance_schema
    initialize_maintenance_schema(connection)
    from .work_delivery import initialize_delivery_schema
    initialize_delivery_schema(connection)
    from .handoffs import initialize_handoff_schema
    initialize_handoff_schema(connection)
    from .task_board import initialize_task_board_schema
    initialize_task_board_schema(connection)
    from .completion_followups import initialize_completion_followups
    initialize_completion_followups(connection)
