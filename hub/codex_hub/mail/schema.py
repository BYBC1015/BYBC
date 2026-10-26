"""Additive schema for application mail, separate from notification recipients."""
import sqlite3


def initialize_mail_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS mail_api_tokens (
          id TEXT PRIMARY KEY,
          owner_id INTEGER NOT NULL REFERENCES users(id),
          name TEXT NOT NULL,
          token_prefix TEXT NOT NULL,
          token_hash TEXT NOT NULL UNIQUE,
          hourly_limit INTEGER NOT NULL DEFAULT 10 CHECK(hourly_limit BETWEEN 1 AND 1000),
          daily_limit INTEGER NOT NULL DEFAULT 50 CHECK(daily_limit BETWEEN 1 AND 1000),
          quota_exempt INTEGER NOT NULL DEFAULT 0 CHECK(quota_exempt IN (0,1)),
          created_at TEXT NOT NULL,
          last_used_at TEXT,
          revoked_at TEXT
        );
        CREATE TABLE IF NOT EXISTS mail_requests (
          id TEXT PRIMARY KEY,
          token_id TEXT NOT NULL REFERENCES mail_api_tokens(id),
          owner_id INTEGER NOT NULL REFERENCES users(id),
          idempotency_key TEXT NOT NULL,
          request_hash TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(token_id, idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS mail_deliveries (
          id TEXT PRIMARY KEY,
          request_id TEXT NOT NULL REFERENCES mail_requests(id),
          token_id TEXT NOT NULL REFERENCES mail_api_tokens(id),
          owner_id INTEGER NOT NULL REFERENCES users(id),
          address TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'queued',
          priority INTEGER NOT NULL DEFAULT 10,
          intent TEXT NOT NULL DEFAULT 'api',
          payload_json TEXT NOT NULL,
          payload_hash TEXT NOT NULL,
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
          UNIQUE(request_id, address)
        );
        CREATE INDEX IF NOT EXISTS idx_mail_delivery_queue
          ON mail_deliveries(status,next_attempt_at,priority,created_at);
        CREATE INDEX IF NOT EXISTS idx_mail_delivery_provider ON mail_deliveries(provider_id);
        CREATE INDEX IF NOT EXISTS idx_mail_delivery_token ON mail_deliveries(token_id,created_at);
        CREATE INDEX IF NOT EXISTS idx_mail_delivery_created ON mail_deliveries(created_at);
        CREATE INDEX IF NOT EXISTS idx_mail_delivery_address ON mail_deliveries(address,status);
        CREATE TABLE IF NOT EXISTS mail_delivery_attempts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          delivery_id TEXT NOT NULL REFERENCES mail_deliveries(id),
          lease_token TEXT NOT NULL,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          outcome TEXT NOT NULL DEFAULT '',
          provider_id TEXT,
          error_code TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS mail_webhook_receipts (
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
          UNIQUE(provider,event_id)
        );
        CREATE INDEX IF NOT EXISTS idx_mail_webhook_pending
          ON mail_webhook_receipts(provider_id,processed_at);
        CREATE TABLE IF NOT EXISTS mail_suppressions (
          address TEXT PRIMARY KEY,
          reason TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
    """)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(mail_api_tokens)")}
    if "quota_exempt" not in columns:
        connection.execute("""ALTER TABLE mail_api_tokens ADD COLUMN
            quota_exempt INTEGER NOT NULL DEFAULT 0 CHECK(quota_exempt IN (0,1))""")
    connection.commit()
