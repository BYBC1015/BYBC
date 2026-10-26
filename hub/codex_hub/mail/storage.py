"""Atomic admission, application credentials, and delivery evidence for mail."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from ..notifications.config import valid_sender
from ..notifications.storage import after, encode, normalize_address, utc_now

MAX_REQUEST_BYTES = 256 * 1024
MAX_CONTENT_BYTES = 128 * 1024
TOKEN_COLUMNS = "id,owner_id,name,token_prefix,hourly_limit,daily_limit,quota_exempt,created_at,last_used_at,revoked_at"
SUPPRESSION_REASONS = frozenset({"hard_bounce", "bounced", "complained", "suppressed", "recipient_rejected"})


class MailError(ValueError):
    def __init__(self, code: str, status_code: int = 422):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def validate_idempotency_key(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[\x21-\x7e]{8,128}", value):
        raise MailError("invalid_idempotency_key")
    return value


def validate_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) - {"to", "subject", "text", "html", "reply_to"}:
        raise MailError("invalid_fields")
    recipients = payload.get("to")
    if isinstance(recipients, str):
        recipients = [recipients]
    if not isinstance(recipients, list) or not 1 <= len(recipients) <= 20:
        raise MailError("invalid_recipients")
    try:
        addresses = list(dict.fromkeys(normalize_address(value) for value in recipients))
    except (ValueError, TypeError):
        raise MailError("invalid_recipients") from None
    subject = payload.get("subject")
    if (not isinstance(subject, str) or not subject.strip() or len(subject) > 200
            or any(ord(c) < 32 or ord(c) == 127 for c in subject)):
        raise MailError("invalid_subject")
    clean: dict[str, Any] = {"to": addresses, "subject": subject}
    has_body = False
    for field in ("text", "html"):
        if field in payload:
            value = payload[field]
            if not isinstance(value, str) or "\x00" in value:
                raise MailError("invalid_content")
            clean[field] = value
            has_body = has_body or bool(value.strip())
    if not has_body:
        raise MailError("content_required")
    if "reply_to" in payload:
        try:
            clean["reply_to"] = normalize_address(payload["reply_to"])
        except (ValueError, TypeError):
            raise MailError("invalid_reply_to") from None
    try:
        body_bytes = sum(len(clean.get(field, "").encode("utf-8")) for field in ("text", "html"))
        request_bytes = len(encode(clean).encode("utf-8"))
    except UnicodeError:
        raise MailError("invalid_content") from None
    if body_bytes > MAX_CONTENT_BYTES or request_bytes > MAX_REQUEST_BYTES:
        raise MailError("content_too_large", 413)
    return clean


class MailStore:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    @contextmanager
    def transaction(self):
        own = not self.connection.in_transaction
        savepoint = "mail_" + uuid.uuid4().hex
        self.connection.execute("BEGIN IMMEDIATE" if own else f"SAVEPOINT {savepoint}")
        try:
            yield
            if own:
                self.connection.commit()
            else:
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        except BaseException:
            if own:
                self.connection.rollback()
            else:
                self.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise

    def _owner(self, owner_id: int) -> None:
        if not self.connection.execute("SELECT 1 FROM users WHERE id=? AND role='owner'", (owner_id,)).fetchone():
            raise MailError("owner_required", 403)

    @staticmethod
    def _limit_value(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
            raise MailError("invalid_limit")
        return value

    def create_token(self, owner_id: int, name: str, *, hourly_limit: int = 10,
                     daily_limit: int = 50, quota_exempt: bool = False) -> dict[str, Any]:
        if (not isinstance(name, str) or not name.strip() or len(name) > 80
                or any(ord(c) < 32 or ord(c) == 127 for c in name)):
            raise MailError("invalid_token_name")
        hourly_limit, daily_limit = self._limit_value(hourly_limit), self._limit_value(daily_limit)
        if not isinstance(quota_exempt, bool):
            raise MailError("invalid_quota_mode")
        raw = "hmail_" + secrets.token_urlsafe(32)
        token_id, now = uuid.uuid4().hex, utc_now()
        with self.transaction():
            self._owner(owner_id)
            self.connection.execute("""INSERT INTO mail_api_tokens
                (id,owner_id,name,token_prefix,token_hash,hourly_limit,daily_limit,quota_exempt,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""", (token_id, owner_id, name.strip(), raw[:14],
                    hashlib.sha256(raw.encode()).hexdigest(), hourly_limit, daily_limit, int(quota_exempt), now))
        return {**dict(self.connection.execute(f"SELECT {TOKEN_COLUMNS} FROM mail_api_tokens WHERE id=?", (token_id,)).fetchone()), "raw": raw}

    def list_tokens(self, owner_id: int) -> list[dict[str, Any]]:
        self._owner(owner_id)
        return [dict(row) for row in self.connection.execute(
            f"SELECT {TOKEN_COLUMNS} FROM mail_api_tokens WHERE owner_id=? ORDER BY created_at DESC,rowid DESC", (owner_id,))]

    def authenticate_token(self, raw_token: str) -> dict[str, Any] | None:
        if not isinstance(raw_token, str) or not re.fullmatch(r"hmail_[A-Za-z0-9_-]{43}", raw_token):
            return None
        with self.transaction():
            row = self.connection.execute(f"SELECT {TOKEN_COLUMNS} FROM mail_api_tokens WHERE token_hash=? AND revoked_at IS NULL",
                (hashlib.sha256(raw_token.encode()).hexdigest(),)).fetchone()
            if not row or not self.connection.execute("SELECT 1 FROM users WHERE id=? AND role='owner'", (row["owner_id"],)).fetchone():
                return None
            now = utc_now()
            self.connection.execute("UPDATE mail_api_tokens SET last_used_at=? WHERE id=?", (now, row["id"]))
            return {**dict(row), "last_used_at": now}

    def _cancel_pending(self, predicate: str, params: list[Any], reason: str, now: str,
                        *, table: str = "mail_deliveries") -> None:
        if table not in {"mail_deliveries", "email_deliveries"}:
            raise ValueError("invalid queue")
        self.connection.execute(f"""UPDATE {table} SET status=CASE
            WHEN uncertain_submission=1 OR status='uncertain' OR (status='sending' AND first_attempt_at IS NOT NULL)
            THEN 'uncertain' ELSE 'cancelled' END,last_error=?,updated_at=?,lease_token=NULL,lease_expires_at=NULL
            WHERE status IN ('queued','retry_wait','sending','uncertain') AND """ + predicate,
            [reason, now, *params])

    def revoke_token(self, owner_id: int, token_id: str) -> None:
        with self.transaction():
            self._owner(owner_id)
            now = utc_now()
            row = self.connection.execute("SELECT id FROM mail_api_tokens WHERE id=? AND owner_id=?", (token_id, owner_id)).fetchone()
            if not row:
                raise MailError("token_not_found", 404)
            self.connection.execute("UPDATE mail_api_tokens SET revoked_at=COALESCE(revoked_at,?) WHERE id=?", (now, token_id))
            self._cancel_pending("token_id=?", [token_id], "token_revoked", now)

    def enqueue(self, token_id: str, payload: dict[str, Any], idempotency_key: str, *,
                from_address: str, hourly_limit: int = 30, daily_limit: int = 100) -> dict[str, Any]:
        clean = validate_payload(payload)
        key = validate_idempotency_key(idempotency_key)
        if not valid_sender(from_address):
            raise MailError("sender_unavailable", 503)
        self._limit_value(hourly_limit)
        self._limit_value(daily_limit)
        serialized = encode(clean)
        request_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        with self.transaction():
            token = self.connection.execute("SELECT * FROM mail_api_tokens WHERE id=? AND revoked_at IS NULL", (token_id,)).fetchone()
            if not token:
                raise MailError("invalid_token", 401)
            self._owner(token["owner_id"])
            existing = self.connection.execute("SELECT * FROM mail_requests WHERE token_id=? AND idempotency_key=?", (token_id, key)).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise MailError("idempotency_conflict", 409)
                return {**self._with_deliveries(existing), "replayed": True}
            if any(self.is_suppressed(address) for address in clean["to"]):
                raise MailError("recipient_suppressed", 409)
            now = utc_now()
            if not token["quota_exempt"]:
                for window, global_limit, token_limit in (({"hours": -1}, hourly_limit, token["hourly_limit"]),
                        ({"days": -1}, daily_limit, token["daily_limit"])):
                    start = after(now, **window)
                    counts = self.connection.execute("""SELECT COUNT(*) AS total,
                        COALESCE(SUM(CASE WHEN d.token_id=? THEN 1 ELSE 0 END),0) AS token_total
                        FROM mail_deliveries d JOIN mail_api_tokens t ON t.id=d.token_id
                        WHERE t.quota_exempt=0 AND d.created_at>?""", (token_id, start)).fetchone()
                    if counts["total"] + len(clean["to"]) > global_limit or counts["token_total"] + len(clean["to"]) > token_limit:
                        raise MailError("quota_exceeded", 429)
            request_id = uuid.uuid4().hex
            self.connection.execute("""INSERT INTO mail_requests
                (id,token_id,owner_id,idempotency_key,request_hash,payload_json,created_at) VALUES(?,?,?,?,?,?,?)""",
                (request_id, token_id, token["owner_id"], key, request_hash, serialized, now))
            for address in clean["to"]:
                delivery_id = uuid.uuid4().hex
                provider_payload = {"from": from_address, **clean, "to": [address],
                    "tags": [{"name": "hub_mail_delivery", "value": delivery_id}]}
                stored = encode(provider_payload)
                self.connection.execute("""INSERT INTO mail_deliveries
                    (id,request_id,token_id,owner_id,address,payload_json,payload_hash,idempotency_key,
                     next_attempt_at,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (delivery_id, request_id, token_id, token["owner_id"], address, stored,
                     hashlib.sha256(stored.encode("utf-8")).hexdigest(), "hub-mail-" + delivery_id,
                     now, after(now, hours=24), now, now))
            row = self.connection.execute("SELECT * FROM mail_requests WHERE id=?", (request_id,)).fetchone()
            return {**self._with_deliveries(row), "replayed": False}

    def _with_deliveries(self, row: sqlite3.Row) -> dict[str, Any]:
        return {**dict(row), "deliveries": [dict(item) for item in self.connection.execute(
            "SELECT * FROM mail_deliveries WHERE request_id=? ORDER BY rowid", (row["id"],))]}

    def get_request(self, token_id: str, request_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM mail_requests WHERE id=? AND token_id=?", (request_id, token_id)).fetchone()
        return self._with_deliveries(row) if row else None

    def list_requests(self, token_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return [self._with_deliveries(row) for row in self.connection.execute(
            "SELECT * FROM mail_requests WHERE token_id=? ORDER BY created_at DESC,rowid DESC LIMIT ?",
            (token_id, min(200, max(1, int(limit))))).fetchall()]

    def list_owner_requests(self, owner_id: int, limit: int = 50) -> list[dict[str, Any]]:
        self._owner(owner_id)
        return [self._with_deliveries(row) for row in self.connection.execute(
            "SELECT * FROM mail_requests WHERE owner_id=? ORDER BY created_at DESC,rowid DESC LIMIT ?",
            (owner_id, min(200, max(1, int(limit))))).fetchall()]

    def is_suppressed(self, address: str) -> bool:
        return bool(self.connection.execute("SELECT 1 FROM mail_suppressions WHERE address=?", (normalize_address(address),)).fetchone())

    def list_suppressions(self, owner_id: int) -> list[dict[str, Any]]:
        self._owner(owner_id)
        return [dict(row) for row in self.connection.execute("SELECT * FROM mail_suppressions ORDER BY updated_at DESC LIMIT 200")]

    def suppress_address(self, address: str, reason: str, now: str | None = None) -> None:
        """Apply shared recipient protection within the caller's transaction."""
        if not self.connection.in_transaction:
            raise RuntimeError("suppression requires a transaction")
        address = normalize_address(address)
        reason = reason if reason in SUPPRESSION_REASONS else "recipient_rejected"
        now = now or utc_now()
        self.connection.execute("""INSERT INTO mail_suppressions(address,reason,created_at,updated_at)
            VALUES(?,?,?,?) ON CONFLICT(address) DO UPDATE SET
            reason=CASE WHEN mail_suppressions.reason='complained' THEN 'complained' ELSE excluded.reason END,
            updated_at=excluded.updated_at""", (address, reason, now, now))
        self._cancel_pending("address=?", [address], reason, now)
        if self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='email_recipients'").fetchone():
            self.connection.execute("UPDATE email_recipients SET paused_reason=?,updated_at=? WHERE address=?", (reason, now, address))
            self._cancel_pending("address=?", [address], reason, now, table="email_deliveries")

    def resume_address(self, owner_id: int, address: str) -> None:
        address = normalize_address(address)
        with self.transaction():
            self._owner(owner_id)
            self.connection.execute("DELETE FROM mail_suppressions WHERE address=?", (address,))
            if self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='email_recipients'").fetchone():
                marks = ",".join("?" for _ in SUPPRESSION_REASONS)
                self.connection.execute(f"UPDATE email_recipients SET paused_reason='',updated_at=? WHERE address=? AND paused_reason IN ({marks})",
                    (utc_now(), address, *SUPPRESSION_REASONS))

    def ingest_webhook(self, event_id: str, event_type: str, provider_id: str, occurred_at: str, *,
                       hard_bounce: bool = False) -> bool:
        for value, limit in ((event_id, 200), (event_type, 80), (provider_id, 200), (occurred_at, 64)):
            if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < 32 for c in value):
                raise ValueError("invalid webhook identifiers")
        parsed = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("webhook timestamp requires timezone")
        occurred_at = parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
        with self.transaction():
            cursor = self.connection.execute("""INSERT OR IGNORE INTO mail_webhook_receipts
                (event_id,event_type,provider_id,occurred_at,hard_bounce,received_at) VALUES(?,?,?,?,?,?)""",
                (event_id, event_type, provider_id, occurred_at, int(hard_bounce), utc_now()))
            self.reconcile_webhooks(provider_id)
            return bool(cursor.rowcount)

    def reconcile_webhooks(self, provider_id: str) -> None:
        """Replay signed facts after provider association; never commit caller work."""
        delivery = self.connection.execute("SELECT * FROM mail_deliveries WHERE provider_id=?", (provider_id,)).fetchone()
        if not delivery:
            return
        transitions = {"email.sent": "accepted", "email.delivered": "delivered", "email.delivery_delayed": "delivery_delayed",
            "email.bounced": "bounced", "email.complained": "complained", "email.failed": "failed", "email.suppressed": "suppressed"}
        rank = {"accepted": 1, "delivery_delayed": 2, "failed": 3, "delivered": 4, "bounced": 5, "suppressed": 5, "complained": 6}
        state, event_at = delivery["status"], delivery["provider_event_at"]
        for receipt in self.connection.execute("SELECT * FROM mail_webhook_receipts WHERE provider_id=? AND processed_at IS NULL ORDER BY occurred_at,id", (provider_id,)).fetchall():
            target = transitions.get(receipt["event_type"])
            if target and rank.get(target, 0) >= rank.get(state, 0):
                state, event_at = target, max(event_at or "", receipt["occurred_at"])
            now = utc_now()
            self.connection.execute("UPDATE mail_deliveries SET status=?,provider_event_at=?,updated_at=? WHERE id=?", (state, event_at, now, delivery["id"]))
            if receipt["event_type"] == "email.complained":
                self.suppress_address(delivery["address"], "complained", now)
            elif receipt["event_type"] == "email.suppressed" or (receipt["event_type"] == "email.bounced" and receipt["hard_bounce"]):
                self.suppress_address(delivery["address"], "hard_bounce", now)
            self.connection.execute("UPDATE mail_webhook_receipts SET processed_at=?,result=? WHERE id=?",
                (now, "applied" if target else "ignored", receipt["id"]))

    def quarantine_restored_queue(self) -> dict[str, int]:
        with self.transaction():
            now = utc_now()
            revoked = self.connection.execute("UPDATE mail_api_tokens SET revoked_at=? WHERE revoked_at IS NULL", (now,)).rowcount
            rows = self.connection.execute("SELECT id,first_attempt_at,status FROM mail_deliveries WHERE status IN ('queued','retry_wait','sending','uncertain')").fetchall()
            cancelled = uncertain = 0
            for row in rows:
                status = "uncertain" if row["first_attempt_at"] or row["status"] in {"sending", "uncertain"} else "cancelled"
                cancelled += status == "cancelled"
                uncertain += status == "uncertain"
                self.connection.execute("""UPDATE mail_deliveries SET status=?,last_error='backup_restored',
                    uncertain_submission=CASE WHEN ?='uncertain' THEN 1 ELSE uncertain_submission END,
                    lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE id=?""", (status, status, now, row["id"]))
            self.connection.execute("""INSERT INTO email_channel_state(id,paused_reason,updated_at)
                VALUES(1,'backup_restored',?) ON CONFLICT(id) DO UPDATE SET paused_reason='backup_restored',updated_at=excluded.updated_at""", (now,))
        return {"tokens_revoked": revoked, "cancelled": cancelled, "uncertain": uncertain}
