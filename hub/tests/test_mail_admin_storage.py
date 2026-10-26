"""Administrator count exemptions preserve ordinary quotas and delivery safety."""
import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from codex_hub.db import connect_database
from codex_hub.mail.schema import initialize_mail_schema
from codex_hub.mail.storage import MailError, MailStore
from codex_hub.notifications.storage import utc_now
from codex_hub.repository import Repository


SENDER = "Hub <notify@notify.example.com>"
BODY = {"to": "recipient@example.com", "subject": "Application event", "text": "Completed."}


@pytest.fixture
def admin_mail(db):
    initialize_mail_schema(db)
    owner = Repository(db).create_owner_if_missing("admin-mail-owner", "test-password")
    store = MailStore(db)
    normal = store.create_token(owner["id"], "Ordinary script", hourly_limit=1, daily_limit=1)
    admin = store.create_token(owner["id"], "Creator", hourly_limit=1, daily_limit=1, quota_exempt=True)
    return store, owner, normal, admin


def submit(store, token, key="event-id-0001", payload=None):
    return store.enqueue(token["id"], payload or BODY, key, from_address=SENDER,
                         hourly_limit=1, daily_limit=1)


def fails_with(code, action):
    with pytest.raises(MailError) as caught:
        action()
    assert caught.value.code == code


def test_old_schema_migration_keeps_existing_key_data_and_limits(tmp_path):
    connection = sqlite3.connect(tmp_path / "old-mail.db")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript("""
        CREATE TABLE users(id INTEGER PRIMARY KEY,role TEXT NOT NULL);
        INSERT INTO users(id,role) VALUES(1,'owner');
        CREATE TABLE mail_api_tokens(
          id TEXT PRIMARY KEY,owner_id INTEGER NOT NULL REFERENCES users(id),name TEXT NOT NULL,
          token_prefix TEXT NOT NULL,token_hash TEXT NOT NULL UNIQUE,
          hourly_limit INTEGER NOT NULL DEFAULT 10,daily_limit INTEGER NOT NULL DEFAULT 50,
          created_at TEXT NOT NULL,last_used_at TEXT,revoked_at TEXT
        );
    """)
    raw = "hmail_" + "x" * 43
    stamp = utc_now()
    connection.execute("""INSERT INTO mail_api_tokens
        (id,owner_id,name,token_prefix,token_hash,hourly_limit,daily_limit,created_at)
        VALUES('legacy-key',1,'Existing key',?,?,1,1,?)""",
        (raw[:14], hashlib.sha256(raw.encode()).hexdigest(), stamp))
    connection.commit()
    before = dict(connection.execute("SELECT * FROM mail_api_tokens").fetchone())
    try:
        initialize_mail_schema(connection)
        initialize_mail_schema(connection)
        migrated = dict(connection.execute("SELECT * FROM mail_api_tokens").fetchone())
        assert migrated.pop("quota_exempt") == 0
        assert migrated == before
        store = MailStore(connection)
        token = store.authenticate_token(raw)
        assert token["quota_exempt"] == 0
        submit(store, token)
        fails_with("quota_exceeded", lambda: submit(store, token, "next-event-0002"))
        assert connection.execute("SELECT COUNT(*) FROM mail_requests").fetchone()[0] == 1
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE mail_api_tokens SET quota_exempt=2")
        connection.rollback()
    finally:
        connection.close()


def test_metadata_exposes_exemption_without_hash_or_secret(admin_mail):
    store, owner, normal, admin = admin_mail
    assert normal["quota_exempt"] == 0 and admin["quota_exempt"] == 1
    authenticated = store.authenticate_token(admin["raw"])
    assert authenticated["quota_exempt"] == 1
    assert "raw" not in authenticated and "token_hash" not in authenticated
    listed = {item["id"]: item for item in store.list_tokens(owner["id"])}
    assert listed[normal["id"]]["quota_exempt"] == 0
    assert listed[admin["id"]]["quota_exempt"] == 1
    assert all("raw" not in item and "token_hash" not in item for item in listed.values())


def test_creator_exceeds_both_quotas_without_consuming_ordinary_pool(db, admin_mail):
    store, _, normal, admin = admin_mail
    batch = {**BODY, "to": [f"recipient{i}@example.com" for i in range(20)]}
    first = submit(store, admin, "creator-event-0001", batch)
    second = submit(store, admin, "creator-event-0002", batch)
    assert len(first["deliveries"]) == len(second["deliveries"]) == 20
    assert submit(store, normal)["deliveries"][0]["status"] == "queued"
    fails_with("quota_exceeded", lambda: submit(store, normal, "normal-event-0002"))
    assert db.execute("SELECT COUNT(*) FROM mail_deliveries").fetchone()[0] == 41


def test_full_normal_pool_does_not_block_creator_and_normal_tokens_share_pool(admin_mail):
    store, owner, normal, admin = admin_mail
    submit(store, normal)
    another = store.create_token(owner["id"], "Another ordinary key", hourly_limit=10, daily_limit=10)
    fails_with("quota_exceeded", lambda: submit(store, another))
    assert submit(store, admin)["deliveries"][0]["status"] == "queued"


def test_concurrent_creator_admissions_keep_all_individual_deliveries(settings, db, admin_mail):
    _, _, _, admin = admin_mail
    barrier = threading.Barrier(2)

    def concurrent_submit(index):
        connection = connect_database(settings.database_path)
        try:
            barrier.wait(timeout=10)
            return submit(MailStore(connection), admin, f"parallel-event-{index}",
                {**BODY, "to": [f"r{i}@example.com" for i in range(20)]})["id"]
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(concurrent_submit, index) for index in range(2)]
        ids = [future.result(timeout=20) for future in futures]
    assert len(set(ids)) == 2
    assert db.execute("SELECT COUNT(*) FROM mail_requests").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM mail_deliveries").fetchone()[0] == 40


def test_only_owner_can_create_exempt_credentials(db, admin_mail):
    store, _, _, _ = admin_mail
    stamp = utc_now()
    member_id = db.execute("""INSERT INTO users(username,password_hash,role,created_at,updated_at)
        VALUES('member-mail','unused-test-hash','member',?,?)""", (stamp, stamp)).lastrowid
    db.commit()
    count_before = db.execute("SELECT COUNT(*) FROM mail_api_tokens").fetchone()[0]
    fails_with("owner_required", lambda: store.create_token(member_id, "Not allowed", quota_exempt=True))
    fails_with("owner_required", lambda: store.create_token(9999999, "Missing owner", quota_exempt=True))
    assert db.execute("SELECT COUNT(*) FROM mail_api_tokens").fetchone()[0] == count_before


@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_exemption_requires_explicit_boolean(admin_mail, value):
    store, owner, _, _ = admin_mail
    fails_with("invalid_quota_mode", lambda: store.create_token(owner["id"], "Invalid", quota_exempt=value))


def test_creator_cannot_bypass_protocol_suppression_or_idempotency(db, admin_mail):
    store, _, normal, admin = admin_mail
    first = submit(store, admin)
    assert submit(store, admin)["id"] == first["id"]
    fails_with("idempotency_conflict", lambda: submit(store, admin, payload={**BODY, "text": "Different"}))
    fails_with("invalid_fields", lambda: submit(store, normal, payload={**BODY, "quota_exempt": True}))
    fails_with("invalid_fields", lambda: submit(store, admin, payload={**BODY, "from": "other@example.com"}))
    fails_with("invalid_recipients", lambda: submit(store, admin, "oversized-event-001", {**BODY, "to": [f"r{i}@example.com" for i in range(21)]}))
    with store.transaction():
        store.suppress_address("blocked@example.com", "complained")
    fails_with("recipient_suppressed", lambda: submit(store, admin, "suppressed-event-001",
        {**BODY, "to": ["good@example.com", "blocked@example.com"]}))
    assert db.execute("SELECT COUNT(*) FROM mail_requests").fetchone()[0] == 1
    assert store.get_request(normal["id"], first["id"]) is None


def test_revoke_creator_stops_unsent_and_preserves_uncertain(db, admin_mail):
    store, owner, _, admin = admin_mail
    request = submit(store, admin, payload={**BODY, "to": ["queued@example.com", "sending@example.com"]})
    attempted_id = request["deliveries"][1]["id"]
    db.execute("""UPDATE mail_deliveries SET status='sending',first_attempt_at=?,uncertain_submission=1,
        lease_token='existing-lease' WHERE id=?""", (utc_now(), attempted_id))
    db.commit()
    store.revoke_token(owner["id"], admin["id"])
    assert store.authenticate_token(admin["raw"]) is None
    fails_with("invalid_token", lambda: submit(store, admin, "after-revoke-001"))
    rows = store.get_request(admin["id"], request["id"])["deliveries"]
    assert [row["status"] for row in rows] == ["cancelled", "uncertain"]
    assert rows[1]["lease_token"] is None


def test_restore_revokes_creator_and_ordinary_keys_and_quarantines_sends(db, admin_mail):
    store, _, normal, admin = admin_mail
    ordinary = submit(store, normal)
    creator = submit(store, admin)
    db.execute("UPDATE mail_deliveries SET status='retry_wait',first_attempt_at=? WHERE id=?",
        (utc_now(), creator["deliveries"][0]["id"]))
    db.commit()
    assert store.quarantine_restored_queue() == {"tokens_revoked": 2, "cancelled": 1, "uncertain": 1}
    assert store.authenticate_token(admin["raw"]) is None
    assert store.authenticate_token(normal["raw"]) is None
    assert store.get_request(normal["id"], ordinary["id"])["deliveries"][0]["status"] == "cancelled"
    assert store.get_request(admin["id"], creator["id"])["deliveries"][0]["status"] == "uncertain"
    assert db.execute("SELECT paused_reason FROM email_channel_state WHERE id=1").fetchone()[0] == "backup_restored"
    assert not db.execute("PRAGMA foreign_key_check").fetchall()
