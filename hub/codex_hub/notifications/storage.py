from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

EVENT_RULES = {"task.done": "task_done", "task.blocked": "task_blocked", "ai.failed": "ai_failed"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def after(value: str, **delta: float) -> str:
    return (datetime.fromisoformat(value) + timedelta(**delta)).isoformat(timespec="seconds")


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def verification_token(verification_id: str, secret: str) -> str:
    signature = hmac.new(secret.encode(), ("hub-email-verification:" + verification_id).encode(), hashlib.sha256).hexdigest()
    return verification_id + "." + signature


def normalize_address(address: str) -> str:
    """Preserve the case-sensitive local part; only normalize the domain."""
    if not isinstance(address, str) or any(ord(c) < 32 or ord(c) == 127 for c in address):
        raise ValueError("邮箱地址格式不正确")
    value = address.strip()
    if len(value) > 254 or value.count("@") != 1:
        raise ValueError("邮箱地址格式不正确")
    local, domain = value.split("@")
    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}", local) or local.startswith(".") or local.endswith(".") or ".." in local:
        raise ValueError("邮箱地址格式不正确")
    try:
        domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("邮箱地址格式不正确") from exc
    labels = domain.split(".")
    if len(labels) < 2 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
        raise ValueError("邮箱地址格式不正确")
    return local + "@" + domain


class NotificationStore:
    """Persistent owner controls and transactional outbox.

    Public management mutations commit only transactions they start. Event capture
    and webhook reconciliation deliberately never commit their caller's work.
    """

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    @contextmanager
    def transaction(self):
        own = not self.connection.in_transaction
        if own:
            self.connection.execute("BEGIN IMMEDIATE")
        else:
            self.connection.execute("SAVEPOINT email_store")
        try:
            yield
            if own:
                self.connection.commit()
            else:
                self.connection.execute("RELEASE SAVEPOINT email_store")
        except Exception:
            if own:
                self.connection.rollback()
            else:
                self.connection.execute("ROLLBACK TO SAVEPOINT email_store")
                self.connection.execute("RELEASE SAVEPOINT email_store")
            raise

    def _owner(self, owner_id: int) -> None:
        if not self.connection.execute("SELECT 1 FROM users WHERE id = ? AND role = 'owner'", (owner_id,)).fetchone():
            raise ValueError("管理员不存在")

    def get_settings(self, owner_id: int) -> dict[str, Any]:
        self._owner(owner_id)
        row = self.connection.execute("SELECT * FROM email_notification_settings WHERE owner_id = ?", (owner_id,)).fetchone()
        return dict(row) if row else {"owner_id": owner_id, "enabled": 0, "task_done": 1, "task_blocked": 1, "ai_failed": 1, "show_project_name": 1, "version": 0, "enabled_at": None, "updated_at": None}

    def get_recipient(self, owner_id: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM email_recipients WHERE owner_id = ? AND revoked_at IS NULL", (owner_id,)).fetchone()
        return dict(row) if row else None

    def update_settings(self, owner_id: int, *, enabled: bool, task_done: bool = True, task_blocked: bool = True, ai_failed: bool = True, show_project_name: bool = True) -> dict[str, Any]:
        with self.transaction():
            old = self.get_settings(owner_id)
            recipient = self.get_recipient(owner_id)
            if enabled and (not recipient or not recipient["verified_at"] or recipient["paused_reason"]):
                raise ValueError("请先验证可用的收件邮箱")
            now = utc_now()
            enabled_at = now if enabled and not old["enabled"] else old["enabled_at"]
            self.connection.execute("""INSERT INTO email_notification_settings
                (owner_id,enabled,task_done,task_blocked,ai_failed,show_project_name,version,enabled_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(owner_id) DO UPDATE SET
                enabled=excluded.enabled,task_done=excluded.task_done,task_blocked=excluded.task_blocked,
                ai_failed=excluded.ai_failed,show_project_name=excluded.show_project_name,
                version=excluded.version,enabled_at=excluded.enabled_at,updated_at=excluded.updated_at""",
                (owner_id,int(enabled),int(task_done),int(task_blocked),int(ai_failed),int(show_project_name),old["version"]+1,enabled_at,now))
            blocked = list(EVENT_RULES) if not enabled else [event for event, flag in EVENT_RULES.items() if not {"task_done":task_done,"task_blocked":task_blocked,"ai_failed":ai_failed}[flag]]
            if blocked:
                marks = ",".join("?" for _ in blocked)
                self._cancel_pending("owner_id = ? AND intent = 'event' AND event_id IN (SELECT id FROM notification_events WHERE event_type IN (" + marks + "))", [owner_id, *blocked], "notification_disabled")
        return self.get_settings(owner_id)

    def _cancel_pending(self, predicate: str, params: list[Any], reason: str) -> None:
        self.connection.execute("""UPDATE email_deliveries SET status = CASE
            WHEN status = 'uncertain' OR uncertain_submission = 1 OR (status = 'sending' AND first_attempt_at IS NOT NULL)
            THEN 'uncertain' ELSE 'cancelled' END, last_error = ?, updated_at = ?,
            lease_token = NULL, lease_expires_at = NULL
            WHERE status IN ('queued','retry_wait','sending','uncertain') AND """ + predicate,
            [reason, utc_now(), *params])

    def set_recipient(self, owner_id: int, address: str) -> dict[str, Any]:
        address = normalize_address(address)
        with self.transaction():
            self._owner(owner_id)
            current = self.get_recipient(owner_id)
            if current and current["address"] == address:
                return current
            now = utc_now()
            if current:
                self.connection.execute("UPDATE email_recipients SET revoked_at=?,updated_at=? WHERE id=?", (now,now,current["id"]))
                self._cancel_pending("recipient_id = ?", [current["id"]], "recipient_replaced")
            self.connection.execute("UPDATE email_notification_settings SET enabled=0,version=version+1,updated_at=? WHERE owner_id=?", (now,owner_id))
            version = self.connection.execute("SELECT COALESCE(MAX(address_version),0)+1 FROM email_recipients WHERE owner_id=?", (owner_id,)).fetchone()[0]
            self.connection.execute("INSERT INTO email_recipients(id,owner_id,address,address_version,created_at,updated_at) VALUES (?,?,?,?,?,?)", (uuid.uuid4().hex,owner_id,address,version,now,now))
        return self.get_recipient(owner_id)

    def revoke_recipient(self, owner_id: int) -> None:
        with self.transaction():
            self._owner(owner_id)
            now = utc_now()
            self.connection.execute("UPDATE email_recipients SET revoked_at=?,updated_at=? WHERE owner_id=? AND revoked_at IS NULL", (now,now,owner_id))
            self.connection.execute("UPDATE email_notification_settings SET enabled=0,version=version+1,updated_at=? WHERE owner_id=?", (now,owner_id))
            self._cancel_pending("owner_id = ?", [owner_id], "recipient_revoked")

    def resume_recipient(self, owner_id: int) -> None:
        with self.transaction():
            self._owner(owner_id)
            recipient = self.get_recipient(owner_id)
            if recipient:
                from ..mail.storage import MailStore
                MailStore(self.connection).resume_address(owner_id, recipient["address"])
            self.connection.execute("UPDATE email_recipients SET paused_reason='',updated_at=? WHERE owner_id=? AND revoked_at IS NULL", (utc_now(),owner_id))

    def quarantine_restored_queue(self) -> dict[str, int]:
        """A restored backup cannot establish whether historical requests were sent."""
        with self.transaction():
            now = utc_now()
            owners = self.connection.execute("UPDATE email_notification_settings SET enabled=0,enabled_at=NULL,version=version+1,updated_at=?", (now,)).rowcount
            pending = self.connection.execute("""SELECT id,first_attempt_at,status FROM email_deliveries
                WHERE status IN ('queued','retry_wait','sending','uncertain')""").fetchall()
            cancelled = uncertain = 0
            for row in pending:
                state = "uncertain" if row["first_attempt_at"] or row["status"] in {"sending","uncertain"} else "cancelled"
                cancelled += state == "cancelled"
                uncertain += state == "uncertain"
                self.connection.execute("""UPDATE email_deliveries SET status=?,last_error='backup_restored',
                    uncertain_submission=CASE WHEN ?='uncertain' THEN 1 ELSE uncertain_submission END,
                    lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE id=?""", (state,state,now,row["id"]))
            self.connection.execute("UPDATE email_verifications SET used_at=? WHERE used_at IS NULL", (now,))
            self.connection.execute("""INSERT INTO email_channel_state(id,paused_reason,updated_at)
                VALUES(1,'backup_restored',?) ON CONFLICT(id) DO UPDATE SET paused_reason='backup_restored',updated_at=excluded.updated_at""", (now,))
        return {"owners_disabled":owners,"cancelled":cancelled,"uncertain":uncertain}

    def get_channel_state(self) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM email_channel_state WHERE id=1").fetchone()
        return dict(row) if row else {"id":1,"paused_reason":"","last_request_at":None,"updated_at":None}

    def resume_channel(self, owner_id: int) -> None:
        with self.transaction():
            self._owner(owner_id)
            now = utc_now()
            self.connection.execute("UPDATE email_channel_state SET paused_reason='',updated_at=? WHERE id=1", (now,))
            self.connection.execute("UPDATE email_notification_settings SET enabled=0,enabled_at=NULL,version=version+1,updated_at=?", (now,))
            self._cancel_pending("intent = 'event'", [], "channel_resumed_requires_opt_in")

    def _limit(self, owner_id: int, intent: str, address: str | None = None) -> None:
        now = utc_now()
        rows = self.connection.execute("""SELECT d.created_at FROM email_deliveries d
            WHERE intent=? AND created_at>=? AND (owner_id=? OR address=?)""",
            (intent,after(now,days=-1),owner_id,address or "")).fetchall()
        if len(rows) >= 5 or any(row[0] > after(now,minutes=-1) for row in rows):
            raise ValueError("发送过于频繁：每分钟最多一次，每天最多五次")

    def request_verification(self, owner_id: int, address: str, *, token_secret: str) -> dict[str, Any]:
        if not token_secret:
            raise ValueError("缺少验证安全配置")
        address = normalize_address(address)
        with self.transaction():
            self._owner(owner_id)
            self._limit(owner_id,"verification",address)
            recipient = self.set_recipient(owner_id,address)
            if recipient["paused_reason"]:
                raise ValueError("请先处理暂停原因并恢复收件邮箱")
            now, verification_id = utc_now(), uuid.uuid4().hex
            token = verification_token(verification_id,token_secret)
            self.connection.execute("UPDATE email_verifications SET used_at=? WHERE recipient_id=? AND used_at IS NULL", (now,recipient["id"]))
            self._cancel_pending("recipient_id = ? AND intent = 'verification'", [recipient["id"]], "verification_replaced")
            self.connection.execute("INSERT INTO email_verifications(id,owner_id,recipient_id,token_hash,expires_at,created_at) VALUES(?,?,?,?,?,?)", (verification_id,owner_id,recipient["id"],hashlib.sha256(token.encode()).hexdigest(),after(now,minutes=30),now))
            delivery = self._manual_event(owner_id,recipient,"verification",{"verification_id":verification_id},after(now,minutes=30))
        return {"token":token,"recipient":recipient,"delivery_id":delivery["id"],"verification_id":verification_id}

    def inspect_verification(self, raw_token: str) -> dict[str, Any] | None:
        if not isinstance(raw_token,str) or len(raw_token)>200:
            return None
        row = self.connection.execute("""SELECT v.*,r.address,r.address_version FROM email_verifications v
            JOIN email_recipients r ON r.id=v.recipient_id
            WHERE v.token_hash=? AND v.used_at IS NULL AND v.expires_at>?
            AND r.revoked_at IS NULL AND r.paused_reason=''""", (hashlib.sha256(raw_token.encode()).hexdigest(),utc_now())).fetchone()
        return dict(row) if row else None

    def confirm_verification(self, raw_token: str) -> bool:
        with self.transaction():
            verification = self.inspect_verification(raw_token)
            if not verification:
                return False
            now = utc_now()
            self.connection.execute("UPDATE email_verifications SET used_at=? WHERE id=?", (now,verification["id"]))
            self.connection.execute("UPDATE email_recipients SET verified_at=?,updated_at=? WHERE id=?", (now,now,verification["recipient_id"]))
        return True

    def enqueue_test(self, owner_id: int) -> dict[str, Any]:
        with self.transaction():
            self._owner(owner_id)
            recipient = self.get_recipient(owner_id)
            if not recipient or not recipient["verified_at"] or recipient["paused_reason"]:
                raise ValueError("请先验证可用的收件邮箱")
            self._limit(owner_id,"test",recipient["address"])
            return self._manual_event(owner_id,recipient,"test",{},after(utc_now(),hours=24))

    def _manual_event(self, owner_id: int, recipient: dict[str,Any], kind: str, payload: dict[str,Any], expires_at: str) -> dict[str,Any]:
        event_id, now = uuid.uuid4().hex, utc_now()
        decision = {"eligible":True,"owner_id":owner_id,"recipient_id":recipient["id"],"recipient_version":recipient["address_version"],"show_project_name":False}
        self.connection.execute("INSERT INTO notification_events(id,owner_id,event_type,entity_type,entity_id,status_version,occurred_at,payload_json,decision_json,decision_reason) VALUES(?,?,?,?,?,0,?,?,?,'eligible')", (event_id,owner_id,kind,kind,event_id,now,encode(payload),encode(decision)))
        return self._queue(event_id,owner_id,recipient,kind,now,expires_at)

    def _queue(self,event_id: str,owner_id: int,recipient: dict[str,Any],intent: str,now: str,expires_at: str,event_type: str = "") -> dict[str,Any]:
        delivery_id = uuid.uuid4().hex
        self.connection.execute("""INSERT OR IGNORE INTO email_deliveries(id,event_id,owner_id,recipient_id,recipient_version,address,priority,intent,idempotency_key,next_attempt_at,expires_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (delivery_id,event_id,owner_id,recipient["id"],recipient["address_version"],recipient["address"],20 if event_type=="task.done" else 10,intent,"hub-email-"+delivery_id,now,expires_at,now,now))
        row = self.connection.execute("SELECT * FROM email_deliveries WHERE event_id=? AND recipient_id=? AND recipient_version=?", (event_id,recipient["id"],recipient["address_version"])).fetchone()
        return dict(row)

    def capture_event(self, *, event_type: str, entity_type: str, entity_id: str | int, status_version: int, project_id: int, occurred_at: str | None = None, deployment_enabled: bool = True) -> None:
        """Append snapshot and queue inside an existing business transaction; never commit."""
        if not self.connection.in_transaction:
            raise RuntimeError("event capture requires a business transaction")
        if event_type not in EVENT_RULES:
            raise ValueError("unknown notification event")
        project = self.connection.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            return
        now = occurred_at or utc_now()
        for owner in self.connection.execute("SELECT id FROM users WHERE role='owner'").fetchall():
            owner_id = owner["id"]
            settings, recipient = self.get_settings(owner_id), self.get_recipient(owner_id)
            reason = "eligible"
            if not deployment_enabled:
                reason = "service_disabled"
            elif not settings["enabled"]:
                reason = "not_enabled"
            elif not settings[EVENT_RULES[event_type]]:
                reason = "rule_disabled"
            elif project["status"] != "active":
                reason = "project_inactive"
            elif not recipient or not recipient["verified_at"] or recipient["paused_reason"]:
                reason = "recipient_unavailable"
            decision = {**settings,"deployment_enabled":bool(deployment_enabled),"eligible":reason=="eligible","recipient_id":recipient["id"] if recipient else None,"recipient_version":recipient["address_version"] if recipient else None}
            payload = {"project_name":re.sub(r"[\x00-\x1f\x7f]"," ",project["name"])[:160] if settings["show_project_name"] else ""}
            event_id = uuid.uuid4().hex
            cursor = self.connection.execute("""INSERT OR IGNORE INTO notification_events(id,owner_id,event_type,entity_type,entity_id,status_version,project_id,occurred_at,payload_json,decision_json,decision_reason)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (event_id,owner_id,event_type,entity_type,str(entity_id),status_version,project_id,now,encode(payload),encode(decision),reason))
            if cursor.rowcount and reason == "eligible":
                self._queue(event_id,owner_id,recipient,"event",now,after(now,hours=24),event_type)

    def list_deliveries(self, owner_id: int, limit: int = 50) -> list[dict[str,Any]]:
        return [dict(row) for row in self.connection.execute("""SELECT d.*,e.event_type,e.occurred_at,e.project_id,e.entity_id
            FROM email_deliveries d JOIN notification_events e ON e.id=d.event_id
            WHERE d.owner_id=? ORDER BY d.created_at DESC,d.rowid DESC LIMIT ?""", (owner_id,min(200,max(1,int(limit))))).fetchall()]

    def get_delivery(self, owner_id: int, delivery_id: str) -> dict[str,Any] | None:
        row = self.connection.execute("""SELECT d.*,e.event_type,e.occurred_at,e.project_id,e.entity_id
            FROM email_deliveries d JOIN notification_events e ON e.id=d.event_id
            WHERE d.owner_id=? AND d.id=?""", (owner_id,delivery_id)).fetchone()
        return dict(row) if row else None

    def get_event(self, owner_id: int, event_id: str) -> dict[str,Any] | None:
        row = self.connection.execute("SELECT * FROM notification_events WHERE owner_id=? AND id=?", (owner_id,event_id)).fetchone()
        return dict(row) if row else None

    def list_attempts(self, owner_id: int, delivery_id: str) -> list[dict[str,Any]]:
        return [dict(row) for row in self.connection.execute("""SELECT a.* FROM email_delivery_attempts a JOIN email_deliveries d ON d.id=a.delivery_id
            WHERE d.owner_id=? AND d.id=? ORDER BY a.id""", (owner_id,delivery_id)).fetchall()]

    def ingest_webhook(self,event_id: str,event_type: str,provider_id: str,occurred_at: str,*,hard_bounce: bool=False) -> bool:
        for value, limit in ((event_id,200),(event_type,80),(provider_id,200)):
            if not isinstance(value,str) or not value or len(value)>limit or any(ord(c)<32 for c in value):
                raise ValueError("invalid webhook identifiers")
        parsed = datetime.fromisoformat(occurred_at.replace("Z","+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("webhook timestamp requires timezone")
        occurred_at = parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
        with self.transaction():
            cursor = self.connection.execute("""INSERT OR IGNORE INTO email_webhook_receipts(event_id,event_type,provider_id,occurred_at,hard_bounce,received_at)
                VALUES(?,?,?,?,?,?)""", (event_id,event_type,provider_id,occurred_at,int(hard_bounce),utc_now()))
            self.reconcile_webhooks(provider_id)
            return bool(cursor.rowcount)

    def reconcile_webhooks(self, provider_id: str) -> None:
        """Merge pending callbacks after provider ID association; never commit."""
        delivery = self.connection.execute("SELECT * FROM email_deliveries WHERE provider_id=?", (provider_id,)).fetchone()
        if not delivery:
            return
        transitions = {"email.sent":"accepted","email.delivered":"delivered","email.delivery_delayed":"delivery_delayed","email.bounced":"bounced","email.complained":"complained","email.failed":"failed","email.suppressed":"bounced"}
        rank = {"accepted":1,"delivery_delayed":2,"failed":3,"delivered":4,"bounced":5,"complained":6}
        state = delivery["status"]
        event_at = delivery["provider_event_at"]
        for receipt in self.connection.execute("SELECT * FROM email_webhook_receipts WHERE provider_id=? AND processed_at IS NULL ORDER BY occurred_at,id", (provider_id,)).fetchall():
            target = transitions.get(receipt["event_type"])
            if target and rank.get(target,0) >= rank.get(state,0):
                state = target
                event_at = max(event_at or "",receipt["occurred_at"])
            pause_reason = ""
            if receipt["event_type"] == "email.complained":
                pause_reason = "complained"
            elif receipt["event_type"] == "email.suppressed" or (receipt["event_type"] == "email.bounced" and receipt["hard_bounce"]):
                pause_reason = "hard_bounce"
            if pause_reason:
                # Protect both sending surfaces while retaining this signed fact.
                from ..mail.storage import MailStore
                self.connection.execute("UPDATE email_deliveries SET status=?,provider_event_at=?,updated_at=? WHERE id=?", (state,event_at,utc_now(),delivery["id"]))
                MailStore(self.connection).suppress_address(delivery["address"], pause_reason)
            self.connection.execute("UPDATE email_webhook_receipts SET processed_at=?,result=? WHERE id=?", (utc_now(),"applied" if target else "ignored",receipt["id"]))
        self.connection.execute("UPDATE email_deliveries SET status=?,provider_event_at=?,updated_at=? WHERE id=?", (state,event_at,utc_now(),delivery["id"]))
