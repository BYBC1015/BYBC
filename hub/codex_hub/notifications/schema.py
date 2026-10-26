"""Additive, repeatable notification schema migration."""

import sqlite3


def initialize_notification_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS email_notification_settings (
          owner_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
          enabled INTEGER NOT NULL DEFAULT 0,
          task_done INTEGER NOT NULL DEFAULT 1,
          task_blocked INTEGER NOT NULL DEFAULT 1,
          ai_failed INTEGER NOT NULL DEFAULT 1,
          show_project_name INTEGER NOT NULL DEFAULT 1,
          version INTEGER NOT NULL DEFAULT 1,
          enabled_at TEXT,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS email_recipients (
          id TEXT PRIMARY KEY,
          owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          address TEXT NOT NULL,
          address_version INTEGER NOT NULL,
          verified_at TEXT,
          revoked_at TEXT,
          paused_reason TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(owner_id, address_version)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_email_current_recipient
          ON email_recipients(owner_id) WHERE revoked_at IS NULL;
        CREATE TABLE IF NOT EXISTS email_verifications (
          id TEXT PRIMARY KEY,
          owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          recipient_id TEXT NOT NULL REFERENCES email_recipients(id),
          token_hash TEXT NOT NULL UNIQUE,
          expires_at TEXT NOT NULL,
          used_at TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notification_events (
          id TEXT PRIMARY KEY,
          owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          event_type TEXT NOT NULL,
          entity_type TEXT NOT NULL,
          entity_id TEXT NOT NULL,
          status_version INTEGER NOT NULL,
          project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
          occurred_at TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          decision_json TEXT NOT NULL,
          decision_reason TEXT NOT NULL,
          UNIQUE(owner_id, entity_type, entity_id, status_version, event_type)
        );
        CREATE TABLE IF NOT EXISTS email_deliveries (
          id TEXT PRIMARY KEY,
          event_id TEXT NOT NULL REFERENCES notification_events(id),
          owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          recipient_id TEXT NOT NULL REFERENCES email_recipients(id),
          recipient_version INTEGER NOT NULL,
          address TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'queued',
          priority INTEGER NOT NULL DEFAULT 10,
          intent TEXT NOT NULL DEFAULT 'event',
          payload_json TEXT,
          payload_hash TEXT,
          idempotency_key TEXT NOT NULL UNIQUE,
          first_attempt_at TEXT,
          next_attempt_at TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          lease_token TEXT,
          lease_expires_at TEXT,
          provider_id TEXT,
          attempt_count INTEGER NOT NULL DEFAULT 0,
          last_error TEXT NOT NULL DEFAULT '',
          uncertain_submission INTEGER NOT NULL DEFAULT 0,
          provider_event_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(event_id, recipient_id, recipient_version)
        );
        CREATE INDEX IF NOT EXISTS idx_email_delivery_queue
          ON email_deliveries(status, next_attempt_at, priority, created_at);
        CREATE INDEX IF NOT EXISTS idx_email_delivery_provider
          ON email_deliveries(provider_id);
        CREATE INDEX IF NOT EXISTS idx_email_delivery_owner
          ON email_deliveries(owner_id, created_at);
        CREATE TABLE IF NOT EXISTS email_delivery_attempts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          delivery_id TEXT NOT NULL REFERENCES email_deliveries(id),
          lease_token TEXT NOT NULL,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          outcome TEXT NOT NULL DEFAULT '',
          provider_id TEXT,
          error_code TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS email_webhook_receipts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          provider TEXT NOT NULL DEFAULT 'resend',
          event_id TEXT NOT NULL,
          event_type TEXT NOT NULL,
          provider_id TEXT NOT NULL,
          occurred_at TEXT NOT NULL,
          hard_bounce INTEGER NOT NULL DEFAULT 0,
          received_at TEXT NOT NULL,
          processed_at TEXT,
          result TEXT NOT NULL DEFAULT 'pending',
          UNIQUE(provider, event_id)
        );
        CREATE INDEX IF NOT EXISTS idx_email_webhook_pending
          ON email_webhook_receipts(provider_id, processed_at);
        CREATE TABLE IF NOT EXISTS email_channel_state (
          id INTEGER PRIMARY KEY CHECK(id = 1),
          paused_reason TEXT NOT NULL DEFAULT '',
          last_request_at TEXT,
          updated_at TEXT NOT NULL
        );
    """)
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
    if "status_version" not in columns:
        connection.execute("ALTER TABLE tasks ADD COLUMN status_version INTEGER NOT NULL DEFAULT 0")
    connection.commit()
