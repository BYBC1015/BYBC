"""Durable script/application mail, independent of Hub notification preferences.

All submissions use an immutable single-recipient payload and idempotency key.
Shared provider pacing is reserved inside SQLite; network I/O never holds a lock.
"""
from __future__ import annotations

import json
import random
import uuid
from datetime import datetime, timedelta
from typing import Callable

from ..config import Settings
from ..db import connect_database
from ..notifications.config import service_ready
from ..notifications.provider import ProviderError, ProviderResult, ResendProvider
from ..notifications.worker import (
    BACKOFF_SECONDS, FACT_RANK, FINAL, LOOKUP_INTERVAL_SECONDS, UTC, _date, _digest,
)
from .storage import MailStore


class MailWorker:
    def __init__(self, settings: Settings, *, provider=None,
                 clock: Callable[[], datetime] | None = None,
                 jitter: Callable[[], float] | None = None):
        self.settings = settings
        self.provider = provider or ResendProvider(settings.email_api_key, timeout=settings.email_timeout_seconds)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.jitter = jitter or random.random

    def close(self) -> None:
        close = getattr(self.provider, "close", None)
        if close:
            close()

    def _eligible(self, db, delivery: dict, now: datetime) -> tuple[bool, str]:
        owner = db.execute("SELECT id FROM users WHERE id=? AND role='owner'",
                           (delivery["owner_id"],)).fetchone()
        if not owner:
            return False, "owner_unavailable"
        token = db.execute("SELECT revoked_at FROM mail_api_tokens WHERE id=? AND owner_id=?",
                           (delivery["token_id"], delivery["owner_id"])).fetchone()
        if not token or token["revoked_at"]:
            return False, "token_revoked"
        if db.execute("SELECT 1 FROM mail_suppressions WHERE address=?",
                      (delivery["address"],)).fetchone():
            return False, "recipient_suppressed"
        if not self.settings.mail_api_enabled or not service_ready(self.settings):
            return False, "service_unavailable"
        channel = db.execute("SELECT paused_reason FROM email_channel_state WHERE id=1").fetchone()
        if channel and channel["paused_reason"]:
            return False, "channel_paused"
        return True, ""

    @staticmethod
    def _payload(delivery: dict) -> dict:
        """Never rebuild a queued message from mutable settings or API requests."""
        payload = json.loads(delivery["payload_json"])
        if (not isinstance(payload, dict) or payload.get("to") != [delivery["address"]]
                or not delivery["payload_hash"] or _digest(payload) != delivery["payload_hash"]):
            raise ValueError("immutable_payload_changed")
        return payload

    def _claim(self) -> dict | None:
        now = self.clock().astimezone(UTC)
        stamp = now.isoformat()
        db = connect_database(self.settings.database_path)
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO email_channel_state(id,updated_at) VALUES(1,?)", (stamp,))
            rows = db.execute("""SELECT d.*,t.quota_exempt FROM mail_deliveries d
                JOIN mail_api_tokens t ON t.id=d.token_id
                WHERE d.status IN ('queued','retry_wait','uncertain','sending','accepted','delivery_delayed')
                  AND d.next_attempt_at<=? AND (d.lease_token IS NULL OR d.lease_expires_at<=?)
                ORDER BY d.priority ASC, d.created_at ASC, d.rowid ASC LIMIT 100""", (stamp, stamp)).fetchall()
            for row in rows:
                delivery = dict(row)
                if delivery["status"] == "uncertain" and not delivery["provider_id"]:
                    # Terminal uncertainty, including quarantined backups, requires
                    # evidence from a callback or operator. Re-enabling is not consent
                    # to resend historical messages with an unknown outcome.
                    self._stop(db, delivery, "uncertain", delivery["last_error"] or "manual_reconciliation_required", stamp)
                    continue
                if delivery["status"] == "sending":
                    # A dead worker may have completed its POST. Retain the original clock and key.
                    delivery["uncertain_submission"] = 1
                    db.execute("UPDATE mail_deliveries SET uncertain_submission=1 WHERE id=?", (delivery["id"],))
                allowed, reason = self._eligible(db, delivery, now)
                provider_id = delivery["provider_id"]
                mode = "lookup" if provider_id else "send"
                uncertain = bool(delivery["uncertain_submission"])
                first = _date(delivery["first_attempt_at"]) if delivery["first_attempt_at"] else None
                if not provider_id and first is None and _date(delivery["expires_at"]) <= now:
                    self._stop(db, delivery, "expired", "queue_expired", stamp)
                    continue
                if not provider_id and not allowed:
                    self._stop(db, delivery, "uncertain" if uncertain else "cancelled", reason, stamp)
                    continue
                if provider_id and not self.settings.email_api_key:
                    self._stop(db, delivery, "uncertain", "lookup_credentials_missing", stamp)
                    continue
                if not provider_id and first and now >= first + timedelta(hours=23):
                    self._stop(db, delivery, "uncertain" if uncertain else "failed", "idempotency_window_expired", stamp)
                    continue
                if not provider_id and delivery["attempt_count"] >= min(6, self.settings.email_max_attempts):
                    self._stop(db, delivery, "uncertain" if uncertain else "failed", "attempts_exhausted", stamp)
                    continue
                channel = db.execute("SELECT last_request_at FROM email_channel_state WHERE id=1").fetchone()
                if channel["last_request_at"]:
                    next_slot = _date(channel["last_request_at"]) + timedelta(seconds=self.settings.email_rate_interval_seconds)
                    if next_slot > now:
                        db.execute("UPDATE mail_deliveries SET next_attempt_at=? WHERE id=?", (next_slot.isoformat(), delivery["id"]))
                        continue
                if not first and mode == "send" and not delivery["quota_exempt"]:
                    for window, limit in ((timedelta(hours=1), self.settings.mail_api_hourly_limit),
                                          (timedelta(days=1), self.settings.mail_api_daily_limit)):
                        count = db.execute("""SELECT COUNT(*) AS n, MIN(d.first_attempt_at) AS oldest
                            FROM mail_deliveries d JOIN mail_api_tokens t ON t.id=d.token_id
                            WHERE t.quota_exempt=0 AND d.first_attempt_at>=?""",
                            ((now - window).isoformat(),)).fetchone()
                        if count["n"] >= limit:
                            next_time = _date(count["oldest"]) + window + timedelta(seconds=1)
                            db.execute("UPDATE mail_deliveries SET next_attempt_at=?,last_error='quota_wait' WHERE id=?",
                                       (next_time.isoformat(), delivery["id"]))
                            break
                    else:
                        count = None
                    if count is not None:
                        continue
                payload = None
                if mode == "send":
                    try:
                        payload = self._payload(delivery)
                    except (ValueError, KeyError, TypeError):
                        self._stop(db, delivery, "uncertain" if uncertain else "failed", "immutable_payload_invalid", stamp)
                        continue
                token = uuid.uuid4().hex
                lease_expiry = (now + timedelta(seconds=max(120, self.settings.email_timeout_seconds * 2 + 30))).isoformat()
                db.execute("""UPDATE mail_deliveries SET status='sending',lease_token=?,lease_expires_at=?,
                    first_attempt_at=CASE WHEN ?='send' THEN COALESCE(first_attempt_at,?) ELSE first_attempt_at END,
                    attempt_count=attempt_count+CASE WHEN ?='send' THEN 1 ELSE 0 END,
                    uncertain_submission=CASE WHEN ?='send' THEN 1 ELSE uncertain_submission END,updated_at=? WHERE id=?""",
                    (token, lease_expiry, mode, stamp, mode, mode, stamp, delivery["id"]))
                db.execute("INSERT INTO mail_delivery_attempts(delivery_id,lease_token,started_at) VALUES(?,?,?)",
                           (delivery["id"], token, stamp))
                db.execute("UPDATE email_channel_state SET last_request_at=?,updated_at=? WHERE id=1", (stamp, stamp))
                db.commit()
                return {**delivery, "lease_token": token, "mode": mode, "payload": payload,
                        "prior_uncertain": uncertain, "attempt_count": delivery["attempt_count"] + (mode == "send")}
            db.commit()
            return None
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _stop(db, delivery: dict, status: str, reason: str, stamp: str) -> None:
        next_time = (_date(stamp) + timedelta(hours=1)).isoformat()
        db.execute("""UPDATE mail_deliveries SET status=?,last_error=?,lease_token=NULL,
            lease_expires_at=NULL,next_attempt_at=?,updated_at=? WHERE id=?""",
            (status, reason, next_time, stamp, delivery["id"]))

    def _still_allowed(self, claim: dict) -> bool:
        """Recheck immediately before POST, including changes after lease acquisition."""
        now = self.clock().astimezone(UTC)
        db = connect_database(self.settings.database_path)
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM mail_deliveries WHERE id=? AND lease_token=? AND lease_expires_at>?",
                             (claim["id"], claim["lease_token"], now.isoformat())).fetchone()
            if not row or row["status"] in FINAL:
                db.commit()
                return False
            allowed, reason = self._eligible(db, dict(row), now)
            if not allowed:
                self._stop(db, dict(row), "uncertain" if claim["prior_uncertain"] else "cancelled", reason, now.isoformat())
                db.execute("UPDATE mail_delivery_attempts SET finished_at=?,outcome='cancelled',error_code=? WHERE lease_token=?",
                           (now.isoformat(), reason, claim["lease_token"]))
            db.commit()
            return allowed
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _late_result(self, db, claim: dict, result: ProviderResult, stamp: str) -> None:
        """Retain immutable acceptance evidence without reviving a fenced lease."""
        row = db.execute("SELECT * FROM mail_deliveries WHERE id=?", (claim["id"],)).fetchone()
        attempt = db.execute("SELECT id FROM mail_delivery_attempts WHERE delivery_id=? AND lease_token=?",
                             (claim["id"], claim["lease_token"])).fetchone()
        if not row or not attempt or row["idempotency_key"] != claim["idempotency_key"]:
            return
        if claim["mode"] == "send" and row["payload_hash"] != _digest(claim["payload"]):
            return
        if row["provider_id"] and row["provider_id"] != result.provider_id:
            return
        if claim["mode"] == "lookup" and claim["provider_id"] != result.provider_id:
            return
        db.execute("""UPDATE mail_delivery_attempts SET finished_at=?,outcome=?,provider_id=?,error_code=''
            WHERE id=?""", (stamp, result.status, result.provider_id, attempt["id"]))
        db.execute("UPDATE mail_deliveries SET provider_id=COALESCE(provider_id,?) WHERE id=?",
                   (result.provider_id, row["id"]))
        if result.status in {"bounced", "complained", "suppressed"}:
            MailStore(db).suppress_address(row["address"], result.status)
            row = db.execute("SELECT * FROM mail_deliveries WHERE id=?", (row["id"],)).fetchone()
        # Acceptance is immutable evidence even if the old lease has merely
        # expired or another worker owns a new one. Keep that lease fenced while
        # merging facts, exactly as a signed callback received during a POST.
        status = max((row["status"], result.status), key=lambda item: FACT_RANK.get(item, 0))
        db.execute("""UPDATE mail_deliveries SET status=?,uncertain_submission=0,updated_at=?
            WHERE id=?""", (status, stamp, row["id"]))
        MailStore(db).reconcile_webhooks(result.provider_id)

    def _finish(self, claim: dict, result: ProviderResult | None, error: ProviderError | None) -> None:
        now = self.clock().astimezone(UTC)
        stamp = now.isoformat()
        db = connect_database(self.settings.database_path)
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM mail_deliveries WHERE id=? AND lease_token=? AND lease_expires_at>?",
                             (claim["id"], claim["lease_token"], stamp)).fetchone()
            if not row:
                if result:
                    self._late_result(db, claim, result, stamp)
                db.commit()
                return
            current = dict(row)
            status = current["status"]
            error_code = ""
            suppression_reason = ""
            uncertain = int(current["uncertain_submission"])
            next_time = now + timedelta(seconds=LOOKUP_INTERVAL_SECONDS)
            provider_id = current["provider_id"]
            if result:
                provider_id = result.provider_id
                status = max((status, claim["status"], result.status), key=lambda item: FACT_RANK.get(item, 0))
                uncertain = 0
                if result.status in {"bounced", "complained", "suppressed"}:
                    # Without a bounce subtype, polling cannot safely assume a temporary bounce.
                    suppression_reason = result.status
            elif error:
                error_code = error.code
                uncertain = int(claim["prior_uncertain"] or error.uncertain or claim["mode"] == "lookup")
                if status not in FINAL:
                    if claim["mode"] == "lookup":
                        # Retrieval cannot cause a second email and may continue after revocation.
                        observed = max((status, claim["status"]), key=lambda item: FACT_RANK.get(item, 0))
                        status = observed if FACT_RANK.get(observed, 0) else "uncertain"
                    elif error.retryable and claim["attempt_count"] < min(6, self.settings.email_max_attempts):
                        status = "retry_wait"
                    else:
                        status = "uncertain" if uncertain else "failed"
                delay = BACKOFF_SECONDS[min(max(claim["attempt_count"] - 1, 0), len(BACKOFF_SECONDS) - 1)]
                if claim["mode"] == "lookup":
                    # Lookup attempts do not increment the POST attempt count;
                    # they must not fall back to a one-minute loop forever.
                    delay = max(delay, LOOKUP_INTERVAL_SECONDS)
                delay = max(delay + delay * 0.15 * self.jitter(), error.retry_after or 0)
                next_time = now + timedelta(seconds=delay)
                # A failed GET does not establish that sending is forbidden.
                # Successful lookup bounce/complaint facts are handled above.
                # API callers supply their own content. A provider validation
                # rejection belongs to this message, unlike a rejected shared
                # credential or sender domain, and must not stop all mail.
                if claim["mode"] == "send" and error.pause_channel and error.code != "request_rejected":
                    db.execute("UPDATE email_channel_state SET paused_reason=?,updated_at=? WHERE id=1", (error.code, stamp))
                if claim["mode"] == "send" and error.pause_recipient:
                    suppression_reason = error.code
            db.execute("""UPDATE mail_deliveries SET status=?,provider_id=?,uncertain_submission=?,
                last_error=?,next_attempt_at=?,lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE id=? AND lease_token=?""",
                (status, provider_id, uncertain, error_code, next_time.isoformat(), stamp, current["id"], claim["lease_token"]))
            db.execute("""UPDATE mail_delivery_attempts SET finished_at=?,outcome=?,provider_id=?,error_code=?
                WHERE delivery_id=? AND lease_token=?""",
                (stamp, result.status if result else status, provider_id, error_code, current["id"], claim["lease_token"]))
            if suppression_reason:
                MailStore(db).suppress_address(current["address"], suppression_reason)
            if provider_id:
                MailStore(db).reconcile_webhooks(provider_id)
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def run_once(self) -> bool:
        claim = self._claim()
        if claim is None:
            return False
        if claim["mode"] == "send" and not self._still_allowed(claim):
            return True
        try:
            if claim["mode"] == "lookup":
                result = self.provider.lookup(claim["provider_id"])
            else:
                result = self.provider.send(claim["payload"], claim["idempotency_key"])
        except ProviderError as error:
            self._finish(claim, None, error)
        else:
            self._finish(claim, result, None)
        return True

