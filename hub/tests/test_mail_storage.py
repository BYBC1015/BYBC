import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from codex_hub.db import connect_database
from codex_hub.mail.schema import initialize_mail_schema
from codex_hub.mail.storage import MailError, MailStore, validate_idempotency_key, validate_payload
from codex_hub.notifications.storage import NotificationStore, utc_now
from codex_hub.repository import Repository


SENDER = "Hub <notify@notify.example.com>"
BODY = {"to": "recipient@example.com", "subject": "Condition triggered", "text": "Your job finished."}


@pytest.fixture
def mail(db):
    initialize_mail_schema(db)
    owner = Repository(db).create_owner_if_missing("mail-owner", "test-password")
    store = MailStore(db)
    token = store.create_token(owner["id"], "Test application")
    return store, owner, token


def enqueue(store, token, key="job-event-0001", payload=None, **kwargs):
    return store.enqueue(token["id"], payload or BODY, key, from_address=SENDER, **kwargs)


def assert_error(code, action):
    with pytest.raises(MailError) as caught:
        action()
    assert caught.value.code == code
    return caught.value


def test_credentials_only_reveal_secret_at_creation_and_have_mail_scope(db, mail):
    store, owner, token = mail
    assert token["raw"].startswith("hmail_") and len(token["raw"]) == 49
    row = db.execute("SELECT * FROM mail_api_tokens").fetchone()
    assert row["token_hash"] == hashlib.sha256(token["raw"].encode()).hexdigest()
    assert token["raw"] not in str(dict(row))
    authenticated = store.authenticate_token(token["raw"])
    assert authenticated["id"] == token["id"] and authenticated["last_used_at"]
    assert "raw" not in authenticated and "token_hash" not in authenticated
    assert "raw" not in store.list_tokens(owner["id"])[0]
    assert "token_hash" not in store.list_tokens(owner["id"])[0]
    assert store.authenticate_token("ch_" + token["raw"][6:]) is None
    assert store.authenticate_token(token["raw"] + "bad") is None


def test_schema_is_repeatable_and_foreign_keys_valid(db, mail):
    store, _, token = mail
    request = enqueue(store, token)
    initialize_mail_schema(db)
    assert store.get_request(token["id"], request["id"])["id"] == request["id"]
    assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_every_recipient_gets_own_immutable_payload_without_binding(db, mail):
    store, _, token = mail
    request = enqueue(store, token, payload={"to": ["First@EXAMPLE.COM", "second@qq.com", "First@example.com"],
        "subject": "工作已完成", "text": "正文", "html": "<p>正文</p>", "reply_to": "reply@EXAMPLE.COM"})
    assert len(request["deliveries"]) == 2
    assert db.execute("SELECT COUNT(*) FROM email_recipients").fetchone()[0] == 0
    for delivery in request["deliveries"]:
        payload = json.loads(delivery["payload_json"])
        assert payload["to"] == [delivery["address"]]
        assert payload["reply_to"] == "reply@example.com" and payload["from"] == SENDER
        assert payload["tags"] == [{"name": "hub_mail_delivery", "value": delivery["id"]}]
        assert delivery["payload_hash"] == hashlib.sha256(delivery["payload_json"].encode()).hexdigest()
        assert delivery["status"] == "queued" and delivery["attempt_count"] == 0
    assert len({d["idempotency_key"] for d in request["deliveries"]}) == 2


def test_notifications_toggle_replace_and_remove_dont_cancel_mail(db, mail):
    store, owner, token = mail
    request = enqueue(store, token)
    notifications = NotificationStore(db)
    notifications.set_recipient(owner["id"], "old@qq.com")
    notifications.update_settings(owner["id"], enabled=False)
    notifications.set_recipient(owner["id"], "new@qq.com")
    notifications.revoke_recipient(owner["id"])
    assert store.get_request(token["id"], request["id"])["deliveries"][0]["status"] == "queued"


def test_same_idempotent_payload_replays_without_new_quota_reservation(db, mail):
    store, owner, _ = mail
    token = store.create_token(owner["id"], "Limited", hourly_limit=1, daily_limit=1)
    first = enqueue(store, token)
    repeated = enqueue(store, token, payload={**BODY, "to": [BODY["to"]]})
    assert repeated["replayed"] and repeated["id"] == first["id"]
    assert db.execute("SELECT COUNT(*) FROM mail_deliveries").fetchone()[0] == 1
    assert_error("idempotency_conflict", lambda: enqueue(store, token, payload={**BODY, "text": "Changed"}))
    assert_error("quota_exceeded", lambda: enqueue(store, token, "job-event-0002"))


def test_mail_query_is_strictly_token_scoped_and_owner_sees_history(mail):
    store, owner, token = mail
    other = store.create_token(owner["id"], "Other application")
    request = enqueue(store, token)
    assert store.get_request(other["id"], request["id"]) is None
    assert store.list_requests(other["id"]) == []
    assert store.list_requests(token["id"])[0]["id"] == request["id"]
    store.revoke_token(owner["id"], token["id"])
    assert store.list_owner_requests(owner["id"])[0]["id"] == request["id"]


def test_pending_deliveries_reserve_recipient_quota_and_rejection_is_atomic(db, mail):
    store, owner, _ = mail
    token = store.create_token(owner["id"], "Batch limited", hourly_limit=2, daily_limit=10)
    enqueue(store, token, payload={**BODY, "to": ["a@example.com", "b@example.com"]})
    assert_error("quota_exceeded", lambda: enqueue(store, token, "next-event-0002", {**BODY, "to": ["c@example.com", "d@example.com"]}))
    assert db.execute("SELECT COUNT(*) FROM mail_requests").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM mail_deliveries").fetchone()[0] == 2


def test_global_quota_cannot_be_bypassed_with_another_token(mail):
    store, owner, first = mail
    second = store.create_token(owner["id"], "Other")
    enqueue(store, first, hourly_limit=1)
    error = assert_error("quota_exceeded", lambda: enqueue(store, second, hourly_limit=1))
    assert error.status_code == 429


def test_daily_quota_applies_across_prior_hours(db, mail):
    store, _, token = mail
    enqueue(store, token, daily_limit=1)
    from codex_hub.notifications.storage import after
    db.execute("UPDATE mail_deliveries SET created_at=?", (after(utc_now(), hours=-2),))
    db.commit()
    assert_error("quota_exceeded", lambda: enqueue(store, token, "next-event-0002", daily_limit=1))


def test_enqueue_respects_outer_transaction_rollback(db, mail):
    store, _, token = mail
    db.execute("BEGIN IMMEDIATE")
    request = enqueue(store, token)
    assert db.in_transaction
    db.rollback()
    assert store.get_request(token["id"], request["id"]) is None
    assert db.execute("SELECT COUNT(*) FROM mail_deliveries").fetchone()[0] == 0


@pytest.mark.parametrize("same_key", [False, True])
def test_concurrent_admission_is_atomic_for_idempotency_and_global_quota(settings, db, mail, same_key):
    store, _, token = mail
    barrier = threading.Barrier(2)

    def submit(index):
        connection = connect_database(settings.database_path)
        try:
            barrier.wait(timeout=10)
            try:
                result = enqueue(MailStore(connection), token, "same-event-0001" if same_key else f"event-number-{index}", hourly_limit=1)
                return result["id"]
            except MailError as exc:
                return exc.code
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit, index) for index in range(2)]
        result = [future.result(timeout=20) for future in futures]
    if same_key:
        assert result[0] == result[1] and result[0] != "quota_exceeded"
    else:
        assert result.count("quota_exceeded") == 1
    assert db.execute("SELECT COUNT(*) FROM mail_requests").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM mail_deliveries").fetchone()[0] == 1


def test_revocation_fences_pending_retains_unknown_and_delivery_facts(db, mail):
    store, owner, token = mail
    request = enqueue(store, token, payload={**BODY, "to": [f"p{i}@example.com" for i in range(4)]})
    ids = [row["id"] for row in request["deliveries"]]
    now = utc_now()
    db.execute("UPDATE mail_deliveries SET status='sending',first_attempt_at=?,lease_token='lease',uncertain_submission=1 WHERE id=?", (now, ids[1]))
    db.execute("UPDATE mail_deliveries SET status='accepted',provider_id='provider-3' WHERE id=?", (ids[2],))
    db.execute("UPDATE mail_deliveries SET status='delivered',provider_id='provider-4' WHERE id=?", (ids[3],))
    db.commit()
    store.revoke_token(owner["id"], token["id"])
    assert store.authenticate_token(token["raw"]) is None
    assert_error("invalid_token", lambda: enqueue(store, token, "new-event-0002"))
    rows = store.get_request(token["id"], request["id"])["deliveries"]
    assert [row["status"] for row in rows] == ["cancelled", "uncertain", "accepted", "delivered"]
    assert rows[1]["lease_token"] is None


def test_owner_must_own_token_to_revoke(db, mail):
    store, _, token = mail
    now = utc_now()
    other_id = db.execute("INSERT INTO users(username,password_hash,role,created_at,updated_at) VALUES('other-owner','unused-test-hash','owner',?,?)", (now, now)).lastrowid
    db.commit()
    assert_error("token_not_found", lambda: store.revoke_token(other_id, token["id"]))
    assert store.authenticate_token(token["raw"])


def test_hard_bounce_suppresses_address_globally_and_preserves_current_fact(db, mail):
    store, owner, token = mail
    other = store.create_token(owner["id"], "Other")
    first = enqueue(store, token)
    pending = enqueue(store, other)
    notifications = NotificationStore(db)
    verification = notifications.request_verification(owner["id"], BODY["to"], token_secret="test-secret")
    delivery = first["deliveries"][0]
    db.execute("UPDATE mail_deliveries SET status='accepted',provider_id='bounce-provider' WHERE id=?", (delivery["id"],))
    db.commit()
    assert store.ingest_webhook("bounce-event", "email.bounced", "bounce-provider", utc_now(), hard_bounce=True)
    assert store.is_suppressed(BODY["to"])
    assert store.get_request(token["id"], first["id"])["deliveries"][0]["status"] == "bounced"
    assert store.get_request(other["id"], pending["id"])["deliveries"][0]["status"] == "cancelled"
    assert notifications.get_delivery(owner["id"], verification["delivery_id"])["status"] == "cancelled"
    assert notifications.get_recipient(owner["id"])["paused_reason"] == "hard_bounce"
    assert_error("recipient_suppressed", lambda: enqueue(store, other, "next-event-0002"))
    store.resume_address(owner["id"], BODY["to"])
    assert not store.is_suppressed(BODY["to"])
    assert notifications.get_recipient(owner["id"])["paused_reason"] == ""
    assert store.get_request(other["id"], pending["id"])["deliveries"][0]["status"] == "cancelled"
    assert enqueue(store, other, "next-event-0002")["deliveries"][0]["status"] == "queued"


def test_suppressed_recipient_rejects_entire_batch_atomically(db, mail):
    store, _, token = mail
    with store.transaction():
        store.suppress_address("bad@example.com", "complained")
    assert_error("recipient_suppressed", lambda: enqueue(store, token, payload={**BODY, "to": ["good@example.com", "bad@example.com"]}))
    assert db.execute("SELECT COUNT(*) FROM mail_requests").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM mail_deliveries").fetchone()[0] == 0


def test_webhook_before_response_reconciles_once_without_status_downgrade(db, mail):
    store, _, token = mail
    request = enqueue(store, token)
    delivery_id = request["deliveries"][0]["id"]
    store.ingest_webhook("early-event", "email.delivered", "provider-early", "2026-10-04T10:00:05Z")
    assert db.execute("SELECT processed_at FROM mail_webhook_receipts").fetchone()[0] is None
    db.execute("UPDATE mail_deliveries SET provider_id='provider-early',status='accepted' WHERE id=?", (delivery_id,))
    store.reconcile_webhooks("provider-early")
    db.commit()
    assert not store.ingest_webhook("early-event", "email.delivered", "provider-early", "2026-10-04T10:00:05Z")
    store.ingest_webhook("late-sent-event", "email.sent", "provider-early", "2026-10-04T10:00:01Z")
    row = store.get_request(token["id"], request["id"])["deliveries"][0]
    assert row["status"] == "delivered"
    assert row["provider_event_at"] == "2026-10-04T10:00:05+00:00"


def test_soft_bounce_does_not_suppress_future_mail(db, mail):
    store, _, token = mail
    request = enqueue(store, token)
    db.execute("UPDATE mail_deliveries SET provider_id='soft-bounce' WHERE id=?", (request["deliveries"][0]["id"],))
    db.commit()
    store.ingest_webhook("soft-event", "email.bounced", "soft-bounce", utc_now(), hard_bounce=False)
    assert not store.is_suppressed(BODY["to"])
    assert enqueue(store, token, "future-event-0001")


def test_restore_revokes_credentials_quarantines_queues_and_pauses_channel(db, mail):
    store, _, token = mail
    request = enqueue(store, token, payload={**BODY, "to": ["a@example.com", "b@example.com", "c@example.com"]})
    ids = [d["id"] for d in request["deliveries"]]
    db.execute("UPDATE mail_deliveries SET status='retry_wait',first_attempt_at=?,lease_token='old-lease' WHERE id=?", (utc_now(), ids[1]))
    db.execute("UPDATE mail_deliveries SET status='delivered',provider_id='done-provider' WHERE id=?", (ids[2],))
    db.commit()
    result = store.quarantine_restored_queue()
    assert result == {"tokens_revoked": 1, "cancelled": 1, "uncertain": 1}
    assert store.authenticate_token(token["raw"]) is None
    rows = store.get_request(token["id"], request["id"])["deliveries"]
    assert [d["status"] for d in rows] == ["cancelled", "uncertain", "delivered"]
    assert rows[1]["uncertain_submission"] == 1 and rows[1]["lease_token"] is None
    assert db.execute("SELECT paused_reason FROM email_channel_state WHERE id=1").fetchone()[0] == "backup_restored"
    assert not db.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("patch,code", [
    ({"from": "attacker@example.com"}, "invalid_fields"),
    ({"to": []}, "invalid_recipients"),
    ({"to": ["a@example.com"] * 21}, "invalid_recipients"),
    ({"to": [1]}, "invalid_recipients"),
    ({"to": "a@example.com\r\nBcc: b@example.com"}, "invalid_recipients"),
    ({"subject": "Hello\nBcc: a@example.com"}, "invalid_subject"),
    ({"subject": "x" * 201}, "invalid_subject"),
    ({"subject": " "}, "invalid_subject"),
    ({"text": None}, "invalid_content"),
    ({"text": "\0"}, "invalid_content"),
    ({"text": " "}, "content_required"),
    ({"text": "汉" * (128 * 1024 // 3 + 1)}, "content_too_large"),
    ({"text": "\ud800"}, "invalid_content"),
    ({"reply_to": "bad"}, "invalid_reply_to"),
])
def test_invalid_payloads_rejected_before_persistence(db, mail, patch, code):
    store, _, token = mail
    assert_error(code, lambda: enqueue(store, token, payload={**BODY, **patch}))
    assert db.execute("SELECT COUNT(*) FROM mail_requests").fetchone()[0] == 0


@pytest.mark.parametrize("key", [None, "short", "x" * 129, "space key", "new\nline", "非ASCII的密钥"])
def test_idempotency_key_validation(key):
    assert_error("invalid_idempotency_key", lambda: validate_idempotency_key(key))


def test_html_only_request_validates_without_rendering_it():
    assert validate_payload({"to": "a@example.com", "subject": "HTML", "html": "<p>body</p>"})["html"] == "<p>body</p>"


@pytest.mark.parametrize("limit", [0, 1001, True, "10"])
def test_token_limits_are_bounded_integers(mail, limit):
    store, owner, _ = mail
    assert_error("invalid_limit", lambda: store.create_token(owner["id"], "Invalid", hourly_limit=limit))
