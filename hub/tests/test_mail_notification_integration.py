"""Shared transport protection without coupling application mail to a primary inbox."""
import base64
import hashlib
import hmac
import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from codex_hub.db import connect_database
from codex_hub.mail.storage import MailStore
from codex_hub.migration import create_backup, restore_backup
from codex_hub.notifications.provider import ProviderResult
from codex_hub.notifications.storage import NotificationStore, utc_now
from codex_hub.notifications.worker import NotificationWorker


ADDRESS = "shared@example.test"
WEBHOOK_KEY = b"isolated-mail-integration-callback-secret"


@pytest.fixture
def integrated(db, repo, settings):
    owner = repo.create_owner_if_missing("integration-owner", "test-password")
    notifications = NotificationStore(db)
    recipient = notifications.set_recipient(owner["id"], ADDRESS)
    db.execute("UPDATE email_recipients SET verified_at=? WHERE id=?", (utc_now(), recipient["id"]))
    db.commit()
    mail = MailStore(db)
    token = mail.create_token(owner["id"], "Integration script")
    settings = replace(settings, email_enabled=True, email_api_key="test-never-networked",
        email_webhook_secret="whsec_" + base64.b64encode(WEBHOOK_KEY).decode(),
        email_domain_verified=True, email_public_base_url="https://hub.example.test/hub",
        email_rate_interval_seconds=0)
    return {"db": db, "owner": owner, "notifications": notifications, "mail": mail,
            "token": token, "settings": settings}


def queue_mail(rig, key="integration-key-1"):
    return rig["mail"].enqueue(rig["token"]["id"],
        {"to": ADDRESS, "subject": "Integration", "text": "Fixture only"}, key,
        from_address=rig["settings"].email_from_address)["deliveries"][0]


def notification_row(rig, delivery_id):
    return dict(rig["db"].execute("SELECT * FROM email_deliveries WHERE id=?", (delivery_id,)).fetchone())


@pytest.mark.parametrize("kind,hard,expected", [
    ("email.bounced", True, "hard_bounce"),
    ("email.complained", False, "complained"),
    ("email.suppressed", False, "hard_bounce"),
])
def test_notification_fact_cancels_application_mail_at_same_address(integrated, kind, hard, expected):
    rig = integrated
    notification = rig["notifications"].enqueue_test(rig["owner"]["id"])
    mail = queue_mail(rig)
    rig["db"].execute("UPDATE email_deliveries SET provider_id='notification-result',status='accepted' WHERE id=?", (notification["id"],))
    rig["db"].commit()
    rig["notifications"].ingest_webhook("shared-fact", kind, "notification-result", utc_now(), hard_bounce=hard)
    assert rig["mail"].is_suppressed(ADDRESS)
    assert rig["notifications"].get_recipient(rig["owner"]["id"])["paused_reason"] == expected
    assert rig["db"].execute("SELECT status FROM mail_deliveries WHERE id=?", (mail["id"],)).fetchone()[0] == "cancelled"
    assert notification_row(rig, notification["id"])["status"] in {"bounced", "complained"}


def test_temporary_bounce_does_not_pause_other_queue(integrated):
    rig = integrated
    notification = rig["notifications"].enqueue_test(rig["owner"]["id"])
    mail = queue_mail(rig)
    rig["db"].execute("UPDATE email_deliveries SET provider_id='temporary-result',status='accepted' WHERE id=?", (notification["id"],))
    rig["db"].commit()
    rig["notifications"].ingest_webhook("temporary-fact", "email.bounced", "temporary-result", utc_now(), hard_bounce=False)
    assert not rig["mail"].is_suppressed(ADDRESS)
    assert rig["db"].execute("SELECT status FROM mail_deliveries WHERE id=?", (mail["id"],)).fetchone()[0] == "queued"


def test_application_fact_blocks_notification_and_owner_resume_does_not_revive_queue(integrated):
    rig = integrated
    notification = rig["notifications"].enqueue_test(rig["owner"]["id"])
    mail = queue_mail(rig)
    rig["db"].execute("UPDATE mail_deliveries SET provider_id='application-result',status='accepted' WHERE id=?", (mail["id"],))
    rig["db"].commit()
    rig["mail"].ingest_webhook("application-fact", "email.complained", "application-result", utc_now())
    assert notification_row(rig, notification["id"])["status"] == "cancelled"
    with pytest.raises(ValueError):
        rig["notifications"].resume_recipient(-1)
    assert rig["mail"].is_suppressed(ADDRESS)
    rig["notifications"].resume_recipient(rig["owner"]["id"])
    assert not rig["mail"].is_suppressed(ADDRESS)
    assert not rig["notifications"].get_recipient(rig["owner"]["id"])["paused_reason"]
    assert notification_row(rig, notification["id"])["status"] == "cancelled"


def test_notification_send_rechecks_shared_suppression_even_without_recipient_flag(integrated):
    rig = integrated
    notification = rig["notifications"].enqueue_test(rig["owner"]["id"])
    now = utc_now()
    # Simulate suppression recorded before this notification recipient existed.
    rig["db"].execute("INSERT INTO mail_suppressions(address,reason,created_at,updated_at) VALUES(?,?,?,?)", (ADDRESS, "complained", now, now))
    rig["db"].commit()

    class Provider:
        def send(self, *args):
            raise AssertionError("Suppressed addresses must never reach transport")

    worker = NotificationWorker(rig["settings"], provider=Provider(),
        clock=lambda: datetime.now(timezone.utc) + timedelta(seconds=1))
    assert not worker.run_once()
    row = notification_row(rig, notification["id"])
    assert row["status"] == "cancelled" and row["last_error"] == "recipient_suppressed"


@pytest.mark.parametrize("status", ["bounced", "complained", "suppressed"])
def test_notification_lookup_protection_preserves_current_result(integrated, status):
    rig = integrated
    notification = rig["notifications"].enqueue_test(rig["owner"]["id"])
    mail = queue_mail(rig)
    rig["db"].execute("UPDATE email_deliveries SET provider_id='looked-up',status='accepted',first_attempt_at=? WHERE id=?", (utc_now(), notification["id"]))
    rig["db"].commit()

    class Provider:
        def lookup(self, provider_id):
            return ProviderResult(provider_id, status)

    worker = NotificationWorker(rig["settings"], provider=Provider(),
        clock=lambda: datetime.now(timezone.utc) + timedelta(seconds=1))
    assert worker.run_once()
    row = notification_row(rig, notification["id"])
    assert row["status"] == status and row["provider_id"] == "looked-up"
    assert row["lease_token"] is None and not row["uncertain_submission"]
    assert rig["mail"].is_suppressed(ADDRESS)
    assert rig["db"].execute("SELECT status FROM mail_deliveries WHERE id=?", (mail["id"],)).fetchone()[0] == "cancelled"


def signed_event():
    event_id = "signed-duplicate-notification-first"
    body = json.dumps({"type": "email.delivered", "created_at": utc_now(),
                       "data": {"email_id": "not-associated-yet"}}).encode()
    timestamp = str(int(time.time()))
    signature = base64.b64encode(hmac.new(WEBHOOK_KEY,
        event_id.encode() + b"." + timestamp.encode() + b"." + body, hashlib.sha256).digest()).decode()
    return event_id, body, {"svix-id": event_id, "svix-timestamp": timestamp,
                            "svix-signature": "v1," + signature, "content-type": "application/json"}


def test_signed_duplicate_in_one_ledger_still_reaches_other(client, app, integrated):
    rig = integrated
    app.state.settings = rig["settings"]
    event_id, body, headers = signed_event()
    event = json.loads(body)
    rig["notifications"].ingest_webhook(event_id, event["type"], event["data"]["email_id"], event["created_at"])
    for _ in range(2):
        assert client.post("/api/webhooks/email/resend", content=body, headers=headers).status_code == 200
    for table in ("email_webhook_receipts", "mail_webhook_receipts"):
        assert rig["db"].execute(f"SELECT COUNT(*) FROM {table} WHERE event_id=?", (event_id,)).fetchone()[0] == 1


def test_webhook_fanout_rolls_back_both_ledgers_on_invalid_second_insert(client, app, integrated, monkeypatch):
    rig = integrated
    app.state.settings = rig["settings"]
    event_id, body, headers = signed_event()

    def invalid_second(*args, **kwargs):
        raise ValueError("invalid second ledger input")

    monkeypatch.setattr(MailStore, "ingest_webhook", invalid_second)
    assert client.post("/api/webhooks/email/resend", content=body, headers=headers).status_code == 400
    for table in ("email_webhook_receipts", "mail_webhook_receipts"):
        assert rig["db"].execute(f"SELECT COUNT(*) FROM {table} WHERE event_id=?", (event_id,)).fetchone()[0] == 0


def test_restore_revokes_application_keys_and_quarantines_both_queues(integrated, tmp_path):
    rig = integrated
    notification = rig["notifications"].enqueue_test(rig["owner"]["id"])
    pending = queue_mail(rig)
    unknown = queue_mail(rig, "integration-unknown-2")
    rig["db"].execute("UPDATE mail_deliveries SET status='sending',first_attempt_at=?,lease_token='old-lease' WHERE id=?", (utc_now(), unknown["id"]))
    rig["db"].commit()
    archive = tmp_path / "both-queues.zip"
    create_backup(rig["settings"].data_dir, archive)
    result = restore_backup(archive, tmp_path / "restored")
    assert result["mail_quarantine"] == {"tokens_revoked": 1, "cancelled": 1, "uncertain": 1}
    restored = connect_database(tmp_path / "restored" / "codex_hub.db")
    try:
        assert MailStore(restored).authenticate_token(rig["token"]["raw"]) is None
        states = {row["id"]: row["status"] for row in restored.execute("SELECT * FROM mail_deliveries")}
        assert states[pending["id"]] == "cancelled" and states[unknown["id"]] == "uncertain"
        assert restored.execute("SELECT COUNT(*) FROM mail_deliveries WHERE lease_token IS NOT NULL").fetchone()[0] == 0
        assert restored.execute("SELECT status FROM email_deliveries WHERE id=?", (notification["id"],)).fetchone()[0] == "cancelled"
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not restored.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        restored.close()


def test_main_rotates_queue_priority_and_closes_both_on_interrupt(integrated, monkeypatch):
    from codex_hub.mail import worker as mail_worker
    from codex_hub.notifications import worker as notification_worker

    order, closed = [], []

    class FirstWorker:
        def __init__(self, settings):
            pass

        def run_once(self):
            order.append("notification")
            raise RuntimeError("Must not prevent the sibling queue from polling")

        def close(self):
            closed.append("notification")
            raise RuntimeError("Must still close sibling")

    class SecondWorker:
        def __init__(self, settings):
            pass

        def run_once(self):
            order.append("mail")

        def close(self):
            closed.append("mail")

    def sleep(delay):
        if len(order) >= 4:
            raise KeyboardInterrupt

    monkeypatch.setattr(notification_worker.Settings, "from_env", lambda: integrated["settings"])
    monkeypatch.setattr(notification_worker, "NotificationWorker", FirstWorker)
    monkeypatch.setattr(mail_worker, "MailWorker", SecondWorker)
    monkeypatch.setattr(notification_worker.time, "sleep", sleep)
    notification_worker.main()
    assert order == ["notification", "mail", "mail", "notification"]
    assert closed == ["notification", "mail"]
