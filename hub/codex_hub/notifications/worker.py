"""Durable notification delivery. Run separately: python -m codex_hub.notifications.worker.

Every network call runs outside a database transaction. A fixed request and the
first-attempt clock are committed before POST; a crashed lease is never evidence
that a message was not accepted by the provider.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..config import Settings
from ..db import connect_database, initialize_schema
from .config import service_ready
from .provider import ProviderError, ProviderResult, ResendProvider
from .templates import render_email

UTC = timezone.utc
TOKEN_PLACEHOLDER = "__HUB_VERIFICATION_TOKEN__"
BACKOFF_SECONDS = (60, 300, 900, 3600, 14400)
LOOKUP_INTERVAL_SECONDS = 600
FINAL = frozenset({"delivered", "bounced", "complained", "suppressed", "failed", "cancelled", "expired"})
FACT_RANK = {"accepted": 1, "delivery_delayed": 2, "failed": 3,
             "delivered": 4, "bounced": 5, "suppressed": 5, "complained": 6}
LOG = logging.getLogger(__name__)


def _date(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(payload: dict) -> str:
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


class NotificationWorker:
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

    def _eligible(self, db, delivery: dict, now: datetime) -> tuple[bool, str, dict, dict]:
        event_row = db.execute("SELECT * FROM notification_events WHERE id=? AND owner_id=?",
                               (delivery["event_id"], delivery["owner_id"])).fetchone()
        recipient = db.execute("SELECT * FROM email_recipients WHERE id=? AND owner_id=?",
                               (delivery["recipient_id"], delivery["owner_id"])).fetchone()
        rule_row = db.execute("SELECT * FROM email_notification_settings WHERE owner_id=?",
                              (delivery["owner_id"],)).fetchone()
        event, rules = dict(event_row or {}), dict(rule_row or {})
        owner = db.execute("SELECT id FROM users WHERE id=? AND role='owner'", (delivery["owner_id"],)).fetchone()
        if not owner:
            return False, "owner_unavailable", event, rules
        if not event or not recipient:
            return False, "recipient_unavailable", event, rules
        from ..mail.storage import MailStore
        if MailStore(db).is_suppressed(delivery["address"]):
            return False, "recipient_suppressed", event, rules
        if (recipient["revoked_at"] or recipient["paused_reason"]
                or recipient["address_version"] != delivery["recipient_version"]
                or recipient["address"] != delivery["address"]):
            return False, "recipient_disabled", event, rules
        if delivery["intent"] == "verification":
            from .storage import verification_token
            verification_id = json.loads(event["payload_json"]).get("verification_id")
            verification = db.execute("SELECT * FROM email_verifications WHERE id=? AND recipient_id=?",
                                      (verification_id, delivery["recipient_id"])).fetchone()
            if not verification or verification["used_at"] or _date(verification["expires_at"]) <= now:
                return False, "verification_expired", event, rules
            derived = verification_token(verification_id, self.settings.secret_key)
            if hashlib.sha256(derived.encode()).hexdigest() != verification["token_hash"]:
                return False, "verification_secret_changed", event, rules
        elif not recipient["verified_at"]:
            return False, "recipient_unverified", event, rules
        if delivery["intent"] == "event":
            column = {"task.done": "task_done", "task.blocked": "task_blocked", "ai.failed": "ai_failed"}.get(event["event_type"])
            if not rules.get("enabled") or not column or not rules.get(column):
                return False, "rule_disabled", event, rules
            project = db.execute("SELECT id FROM projects WHERE id=? AND archived_at IS NULL AND status='active'",
                                 (event["project_id"],)).fetchone()
            if not project:
                return False, "project_unavailable", event, rules
        if not service_ready(self.settings):
            return False, "service_unavailable", event, rules
        channel = db.execute("SELECT paused_reason FROM email_channel_state WHERE id=1").fetchone()
        if channel and channel["paused_reason"]:
            return False, "channel_paused", event, rules
        return True, "", event, rules

    def _payload(self, delivery: dict, event: dict, rules: dict) -> tuple[str, dict, str]:
        """Persist a token-free template; materialize its one-time URL only in RAM."""
        from .storage import verification_token

        snapshot = json.loads(event["payload_json"])
        if delivery["payload_json"]:
            stored = delivery["payload_json"]
        else:
            decision = json.loads(event["decision_json"])
            reveal = bool(decision.get("show_project_name", False) and rules.get("show_project_name", False))
            message = render_email(
                event["event_type"], public_base_url=self.settings.email_public_base_url,
                delivery_id=delivery["id"], occurred_at=event["occurred_at"],
                project_name=snapshot.get("project_name"), show_project_name=reveal,
                verification_token=TOKEN_PLACEHOLDER if delivery["intent"] == "verification" else None,
            )
            stored = _json({"from": self.settings.email_from_address, "to": [delivery["address"]],
                            **message, "tags": [{"name": "hub_delivery", "value": delivery["id"]}]})
        if delivery["intent"] == "verification":
            token = verification_token(snapshot["verification_id"], self.settings.secret_key)
            payload = json.loads(stored)
            # The marker is legal in an email local part and display name. Only
            # the fixed template body contains a token placeholder by contract.
            for field in ("text", "html"):
                payload[field] = payload[field].replace(TOKEN_PLACEHOLDER, token)
        else:
            payload = json.loads(stored)
        digest = _digest(payload)
        if delivery["payload_hash"] and delivery["payload_hash"] != digest:
            raise ValueError("immutable_payload_changed")
        return stored, payload, digest

    def _claim(self) -> dict | None:
        now = self.clock().astimezone(UTC)
        stamp = now.isoformat()
        db = connect_database(self.settings.database_path)
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO email_channel_state(id,updated_at) VALUES(1,?)", (stamp,))
            rows = db.execute("""SELECT * FROM email_deliveries
                WHERE status IN ('queued','retry_wait','uncertain','sending','accepted','delivery_delayed')
                  AND next_attempt_at<=? AND (lease_token IS NULL OR lease_expires_at<=?)
                ORDER BY priority ASC, created_at ASC LIMIT 100""", (stamp, stamp)).fetchall()
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
                    db.execute("UPDATE email_deliveries SET uncertain_submission=1 WHERE id=?", (delivery["id"],))
                allowed, reason, event, rules = self._eligible(db, delivery, now)
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
                        db.execute("UPDATE email_deliveries SET next_attempt_at=? WHERE id=?", (next_slot.isoformat(), delivery["id"]))
                        continue
                if not first and mode == "send":
                    for window, limit in ((timedelta(hours=1), self.settings.email_hourly_limit),
                                          (timedelta(days=1), self.settings.email_daily_limit)):
                        count = db.execute("""SELECT COUNT(*) AS n, MIN(first_attempt_at) AS oldest
                            FROM email_deliveries WHERE recipient_id=? AND first_attempt_at>=?""",
                            (delivery["recipient_id"], (now - window).isoformat())).fetchone()
                        if count["n"] >= limit:
                            next_time = _date(count["oldest"]) + window + timedelta(seconds=1)
                            db.execute("UPDATE email_deliveries SET next_attempt_at=?,last_error='quota_wait' WHERE id=?",
                                       (next_time.isoformat(), delivery["id"]))
                            break
                    else:
                        count = None
                    if count is not None:
                        continue
                payload = None
                if mode == "send":
                    try:
                        stored, payload, digest = self._payload(delivery, event, rules)
                    except (ValueError, KeyError, TypeError):
                        self._stop(db, delivery, "uncertain" if uncertain else "failed", "immutable_payload_invalid", stamp)
                        continue
                    db.execute("UPDATE email_deliveries SET payload_json=?,payload_hash=? WHERE id=?",
                               (stored, digest, delivery["id"]))
                token = uuid.uuid4().hex
                lease_expiry = (now + timedelta(seconds=max(120, self.settings.email_timeout_seconds * 2 + 30))).isoformat()
                db.execute("""UPDATE email_deliveries SET status='sending',lease_token=?,lease_expires_at=?,
                    first_attempt_at=CASE WHEN ?='send' THEN COALESCE(first_attempt_at,?) ELSE first_attempt_at END,
                    attempt_count=attempt_count+CASE WHEN ?='send' THEN 1 ELSE 0 END,
                    uncertain_submission=CASE WHEN ?='send' THEN 1 ELSE uncertain_submission END,updated_at=? WHERE id=?""",
                    (token, lease_expiry, mode, stamp, mode, mode, stamp, delivery["id"]))
                db.execute("INSERT INTO email_delivery_attempts(delivery_id,lease_token,started_at) VALUES(?,?,?)",
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
        db.execute("""UPDATE email_deliveries SET status=?,last_error=?,lease_token=NULL,
            lease_expires_at=NULL,next_attempt_at=?,updated_at=? WHERE id=?""",
            (status, reason, next_time, stamp, delivery["id"]))

    def _still_allowed(self, claim: dict) -> bool:
        """Recheck immediately before POST, including changes after lease acquisition."""
        now = self.clock().astimezone(UTC)
        db = connect_database(self.settings.database_path)
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM email_deliveries WHERE id=? AND lease_token=? AND lease_expires_at>?",
                             (claim["id"], claim["lease_token"], now.isoformat())).fetchone()
            if not row or row["status"] in FINAL:
                db.commit()
                return False
            allowed, reason, _, _ = self._eligible(db, dict(row), now)
            if not allowed:
                self._stop(db, dict(row), "uncertain" if claim["prior_uncertain"] else "cancelled", reason, now.isoformat())
                db.execute("UPDATE email_delivery_attempts SET finished_at=?,outcome='cancelled',error_code=? WHERE lease_token=?",
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
        row = db.execute("SELECT * FROM email_deliveries WHERE id=?", (claim["id"],)).fetchone()
        attempt = db.execute("SELECT id FROM email_delivery_attempts WHERE delivery_id=? AND lease_token=?",
                             (claim["id"], claim["lease_token"])).fetchone()
        if not row or not attempt or row["idempotency_key"] != claim["idempotency_key"]:
            return
        if claim["mode"] == "send" and row["payload_hash"] != _digest(claim["payload"]):
            return
        if row["provider_id"] and row["provider_id"] != result.provider_id:
            return
        if claim["mode"] == "lookup" and claim["provider_id"] != result.provider_id:
            return
        db.execute("""UPDATE email_delivery_attempts SET finished_at=?,outcome=?,provider_id=?,error_code=''
            WHERE id=?""", (stamp, result.status, result.provider_id, attempt["id"]))
        db.execute("UPDATE email_deliveries SET provider_id=COALESCE(provider_id,?) WHERE id=?",
                   (result.provider_id, row["id"]))
        if row["lease_token"] is None:
            # A cancellation revoked the lease. The immutable response proves
            # this particular request was already accepted and cannot be recalled.
            status = max((row["status"], result.status), key=lambda item: FACT_RANK.get(item, 0))
            db.execute("""UPDATE email_deliveries SET status=?,uncertain_submission=0,updated_at=?
                WHERE id=? AND lease_token IS NULL""", (status, stamp, row["id"]))
        if result.status in {"bounced", "complained", "suppressed"}:
            from ..mail.storage import MailStore
            MailStore(db).suppress_address(row["address"], result.status, stamp)
        from .storage import NotificationStore
        NotificationStore(db).reconcile_webhooks(result.provider_id)

    def _finish(self, claim: dict, result: ProviderResult | None, error: ProviderError | None) -> None:
        now = self.clock().astimezone(UTC)
        stamp = now.isoformat()
        db = connect_database(self.settings.database_path)
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM email_deliveries WHERE id=? AND lease_token=? AND lease_expires_at>?",
                             (claim["id"], claim["lease_token"], stamp)).fetchone()
            if not row:
                if result:
                    self._late_result(db, claim, result, stamp)
                db.commit()
                return
            current = dict(row)
            status = current["status"]
            error_code = ""
            uncertain = int(current["uncertain_submission"])
            next_time = now + timedelta(seconds=LOOKUP_INTERVAL_SECONDS)
            provider_id = current["provider_id"]
            suppression_reason = ""
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
                if claim["mode"] == "send" and error.pause_channel:
                    db.execute("UPDATE email_channel_state SET paused_reason=?,updated_at=? WHERE id=1", (error.code, stamp))
                if claim["mode"] == "send" and error.pause_recipient:
                    suppression_reason = error.code
            db.execute("""UPDATE email_deliveries SET status=?,provider_id=?,uncertain_submission=?,
                last_error=?,next_attempt_at=?,lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE id=? AND lease_token=?""",
                (status, provider_id, uncertain, error_code, next_time.isoformat(), stamp, current["id"], claim["lease_token"]))
            db.execute("""UPDATE email_delivery_attempts SET finished_at=?,outcome=?,provider_id=?,error_code=?
                WHERE delivery_id=? AND lease_token=?""",
                (stamp, result.status if result else status, provider_id, error_code, current["id"], claim["lease_token"]))
            if suppression_reason:
                # Persist this result before protection fences other pending leases.
                from ..mail.storage import MailStore
                MailStore(db).suppress_address(current["address"], suppression_reason, stamp)
            if provider_id:
                from .storage import NotificationStore
                NotificationStore(db).reconcile_webhooks(provider_id)
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


def main() -> None:
    # MailWorker shares helpers above; import only after this module is initialized.
    from ..mail.worker import MailWorker

    settings = Settings.from_env()
    settings.ensure_directories()
    db = connect_database(settings.database_path)
    try:
        initialize_schema(db)
    finally:
        db.close()
    workers = [NotificationWorker(settings)]
    logging.basicConfig(level=logging.INFO)
    LOG.info("Email workers started; outbound enabled=%s", service_ready(settings))
    try:
        workers.append(MailWorker(settings))
        first = 0
        while True:
            for worker in workers[first:] + workers[:first]:
                try:
                    worker.run_once()
                except Exception:
                    # A retry recovers committed leases; never log provider/body/secrets.
                    LOG.error("Email worker iteration failed; retrying safely")
            # Both queues share a request slot. Rotate priority to avoid starvation.
            first = 1 - first
            time.sleep(max(0.2, settings.email_poll_seconds))
    except KeyboardInterrupt:
        pass
    finally:
        for worker in workers:
            try:
                worker.close()
            except Exception:
                LOG.error("Email worker cleanup failed")


if __name__ == "__main__":
    main()
