import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from codex_hub.db import connect_database, initialize_schema
from codex_hub.notifications.storage import NotificationStore, normalize_address, utc_now
from codex_hub.repository import Repository


@pytest.fixture
def repo(db):
    return Repository(db, email_notifications_enabled=True)


@pytest.fixture
def seeded(repo, db):
    owner = repo.create_owner_if_missing("email-owner", "test-password")
    project = repo.create_project(slug="email-project", name="Private project", created_by_user_id=owner["id"])
    task = repo.create_task(project["id"], "Private task title")
    store = NotificationStore(db)
    verification = store.request_verification(owner["id"], "Local.Part@Example.COM", token_secret="test-secret")
    assert store.confirm_verification(verification["token"])
    store.update_settings(owner["id"], enabled=True)
    return owner, project, task, store


def event_rows(db):
    return db.execute("SELECT * FROM notification_events WHERE entity_type IN ('task','server_ai_job') ORDER BY rowid").fetchall()


def event_deliveries(db):
    return db.execute("SELECT * FROM email_deliveries WHERE intent='event' ORDER BY rowid").fetchall()


def test_task_outbox_deduplicates_same_status_and_allows_reopen(repo, db, seeded):
    owner, project, task, store = seeded
    repo.update_task_status(task["id"], "done")
    repo.update_task_status(task["id"], "done", "new result must not be mailed")
    assert len(event_rows(db)) == len(event_deliveries(db)) == 1
    repo.update_task_status(task["id"], "open")
    repo.update_task_status(task["id"], "done")
    assert [row["status_version"] for row in event_rows(db)] == [1,3]
    assert len(event_deliveries(db)) == 2
    payload = "".join(row["payload_json"] for row in event_rows(db))
    assert "Private task title" not in payload and "new result" not in payload


def test_creation_in_final_state_is_not_a_notification(repo, db, seeded):
    _, project, _, _ = seeded
    repo.create_task(project["id"], "already completed", status="done")
    repo.create_task(project["id"], "already blocked", status="blocked")
    assert event_rows(db) == []


def test_task_and_outbox_rollback_together_on_failure(repo, db, seeded, monkeypatch):
    _, _, task, _ = seeded
    original = NotificationStore.capture_event

    def fail_after_event(self, **kwargs):
        original(self, **kwargs)
        raise RuntimeError("simulate crash before business commit")

    monkeypatch.setattr(NotificationStore, "capture_event", fail_after_event)
    with pytest.raises(RuntimeError):
        repo.update_task_status(task["id"], "done")
    assert repo.get_task(task["id"])["status"] == "open"
    assert event_rows(db) == [] and event_deliveries(db) == []


def test_outer_transaction_controls_status_and_event_commit(repo, db, seeded):
    _, _, task, _ = seeded
    db.execute("BEGIN IMMEDIATE")
    repo.update_task_status(task["id"], "blocked")
    assert db.in_transaction and len(event_rows(db)) == 1
    db.rollback()
    assert repo.get_task(task["id"])["status"] == "open"
    assert event_rows(db) == []


def test_concurrent_status_change_has_one_event(settings, repo, db, seeded):
    _, _, task, _ = seeded
    barrier = threading.Barrier(2)

    def update():
        connection = connect_database(settings.database_path)
        try:
            barrier.wait(timeout=10)
            Repository(connection, email_notifications_enabled=True).update_task_status(task["id"], "done")
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(update) for _ in range(2)]
        for future in futures:
            future.result(timeout=20)
    assert len(event_rows(db)) == len(event_deliveries(db)) == 1


def test_disabled_events_do_not_replay_and_decisions_are_frozen(repo, db, seeded):
    owner, _, task, store = seeded
    store.update_settings(owner["id"], enabled=False, show_project_name=False)
    repo.update_task_status(task["id"], "blocked")
    skipped = event_rows(db)[0]
    assert skipped["decision_reason"] == "not_enabled"
    assert json.loads(skipped["payload_json"])["project_name"] == ""
    decision = json.loads(skipped["decision_json"])
    assert not decision["enabled"] and not decision["show_project_name"]
    store.update_settings(owner["id"], enabled=True)
    repo.update_task_status(task["id"], "blocked")
    assert len(event_rows(db)) == 1 and event_deliveries(db) == []


def test_address_replacement_cancels_old_queue_and_invalidates_old_link(repo, db, seeded):
    owner, _, task, store = seeded
    repo.update_task_status(task["id"], "done")
    old = event_deliveries(db)[0]
    recipient = store.set_recipient(owner["id"], "new@example.com")
    assert recipient["address_version"] == 2 and recipient["verified_at"] is None
    assert not store.get_settings(owner["id"])["enabled"]
    saved = store.get_delivery(owner["id"], old["id"])
    assert saved["status"] == "cancelled" and saved["address"] == "Local.Part@example.com"
    assert saved["recipient_id"] != recipient["id"]


def test_switch_off_retains_uncertain_and_accepted_delivery_facts(repo, db, seeded):
    owner, _, task, store = seeded
    repo.update_task_status(task["id"], "done")
    first = event_deliveries(db)[0]
    db.execute("UPDATE email_deliveries SET status='retry_wait',first_attempt_at=?,uncertain_submission=1 WHERE id=?", (utc_now(),first["id"]))
    db.commit()
    repo.update_task_status(task["id"], "blocked")
    second = event_deliveries(db)[1]
    db.execute("UPDATE email_deliveries SET status='accepted',provider_id='known' WHERE id=?", (second["id"],))
    db.commit()
    store.update_settings(owner["id"], enabled=False)
    assert store.get_delivery(owner["id"], first["id"])["status"] == "uncertain"
    assert store.get_delivery(owner["id"], second["id"])["status"] == "accepted"


def test_verification_inspection_is_read_only_and_token_is_hashed(repo, db):
    owner = repo.create_owner_if_missing("verify-owner", "test-password")
    store = NotificationStore(db)
    result = store.request_verification(owner["id"], "A@Example.COM", token_secret="secret")
    token = result["token"]
    for _ in range(2):
        assert store.inspect_verification(token)["owner_id"] == owner["id"]
    assert not store.get_recipient(owner["id"])["verified_at"]
    stored = db.execute("SELECT * FROM email_verifications").fetchone()
    assert stored["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in "\n".join(db.iterdump())
    assert store.confirm_verification(token)
    assert not store.confirm_verification(token)
    assert not store.get_settings(owner["id"])["enabled"]


def test_old_address_token_invalid_after_replacement_and_revoke(repo, db):
    owner = repo.create_owner_if_missing("verify-owner", "test-password")
    store = NotificationStore(db)
    result = store.request_verification(owner["id"], "old@example.com", token_secret="secret")
    store.set_recipient(owner["id"], "new@example.com")
    assert store.inspect_verification(result["token"]) is None
    assert not store.confirm_verification(result["token"])
    store.revoke_recipient(owner["id"])
    assert store.get_recipient(owner["id"]) is None


def test_verification_and_test_rate_limits(repo, db, seeded):
    owner, _, _, store = seeded
    with pytest.raises(ValueError, match="频繁"):
        store.request_verification(owner["id"], "different@example.com", token_secret="secret")
    assert store.get_recipient(owner["id"])["address"] == "Local.Part@example.com"
    store.enqueue_test(owner["id"])
    with pytest.raises(ValueError, match="频繁"):
        store.enqueue_test(owner["id"])


@pytest.mark.parametrize("address", ["a@example.com\r\nBcc:b@example.com","a\n@example.com","a b@example.com",".a@example.com","a..b@example.com","a@localhost","a@example..com","a@-example.com"])
def test_invalid_addresses_rejected(address):
    with pytest.raises(ValueError):
        normalize_address(address)


def test_address_normalization_preserves_local_part():
    assert normalize_address("  Case.Sensitive+tag@EXAMPLE.COM  ") == "Case.Sensitive+tag@example.com"


def test_early_callback_association_dedup_and_out_of_order(repo, db, seeded):
    owner, _, task, store = seeded
    repo.update_task_status(task["id"], "done")
    delivery = event_deliveries(db)[0]
    assert store.ingest_webhook("event-delivered", "email.delivered", "external-id", "2026-10-04T10:00:00Z")
    assert not store.ingest_webhook("event-delivered", "email.delivered", "external-id", "2026-10-04T10:00:00Z")
    assert store.get_delivery(owner["id"],delivery["id"])["status"] == "queued"
    with store.transaction():
        db.execute("UPDATE email_deliveries SET provider_id='external-id',status='accepted' WHERE id=?", (delivery["id"],))
        store.reconcile_webhooks("external-id")
    assert store.get_delivery(owner["id"],delivery["id"])["status"] == "delivered"
    store.ingest_webhook("late-sent", "email.sent", "external-id", "2026-10-04T09:59:59Z")
    assert store.get_delivery(owner["id"],delivery["id"])["status"] == "delivered"


@pytest.mark.parametrize("event_type,hard_bounce,reason", [("email.bounced",True,"hard_bounce"),("email.complained",False,"complained"),("email.suppressed",False,"hard_bounce")])
def test_hard_bounce_or_complaint_pauses_and_cancels_remaining(repo, db, seeded,event_type,hard_bounce,reason):
    owner, _, task, store = seeded
    repo.update_task_status(task["id"],"done")
    repo.update_task_status(task["id"],"blocked")
    first,second = event_deliveries(db)
    db.execute("UPDATE email_deliveries SET provider_id='external-id',status='delivered' WHERE id=?", (first["id"],))
    db.commit()
    store.ingest_webhook("bounce-or-complaint",event_type,"external-id","2026-10-04T10:00:00Z",hard_bounce=hard_bounce)
    assert store.get_recipient(owner["id"])["paused_reason"] == reason
    assert store.get_delivery(owner["id"],second["id"])["status"] == "cancelled"
    store.resume_recipient(owner["id"])
    assert store.get_recipient(owner["id"])["paused_reason"] == ""
    assert store.get_delivery(owner["id"],second["id"])["status"] == "cancelled"


def test_restored_backup_quarantines_history_without_destroying_facts(repo, db, seeded):
    owner, _, task, store = seeded
    repo.update_task_status(task["id"],"done")
    repo.update_task_status(task["id"],"blocked")
    repo.update_task_status(task["id"],"done")
    first,second,third = event_deliveries(db)
    db.execute("UPDATE email_deliveries SET status='retry_wait',first_attempt_at=? WHERE id=?", (utc_now(),second["id"]))
    db.execute("UPDATE email_deliveries SET status='delivered' WHERE id=?", (third["id"],))
    db.commit()
    result = store.quarantine_restored_queue()
    assert result["owners_disabled"] == 1 and result["uncertain"] == 1
    assert store.get_delivery(owner["id"],first["id"])["status"] == "cancelled"
    assert store.get_delivery(owner["id"],second["id"])["status"] == "uncertain"
    assert store.get_delivery(owner["id"],third["id"])["status"] == "delivered"
    assert not store.get_settings(owner["id"])["enabled"]
    assert db.execute("SELECT paused_reason FROM email_channel_state").fetchone()[0] == "backup_restored"


def test_schema_upgrade_is_repeatable_and_preserves_tasks(repo, db, seeded):
    _,_,task,_ = seeded
    db.execute("ALTER TABLE tasks DROP COLUMN status_version")
    db.commit()
    assert "status_version" not in {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
    initialize_schema(db)
    initialize_schema(db)
    assert repo.get_task(task["id"])["title"] == "Private task title"
    assert repo.get_task(task["id"])["status_version"] == 0


def make_ai_job(repo, owner, project):
    device = repo.ensure_server_ai_device("deepseek", "DeepSeek")
    repo.add_project_member(project["id"], device["id"], "write")
    entry = repo.create_entry(project_id=project["id"], author_user_id=owner["id"], source_label="owner", kind="message", body_markdown="请 @DeepSeek 处理")
    return repo.enqueue_server_ai_jobs_for_entry(entry["id"], {"deepseek"})[0]


def test_ai_retries_do_not_notify_but_first_final_failure_does(repo, db, seeded):
    owner,project,_,_ = seeded
    job = make_ai_job(repo,owner,project)
    claimed = repo.claim_next_server_ai_job({"deepseek"})
    assert claimed["id"] == job["id"]
    repo.reschedule_server_ai_job(job["id"], utc_now(), "temporary error details")
    assert event_rows(db) == []
    repo.claim_next_server_ai_job({"deepseek"})
    response = repo.finish_server_ai_job(job["id"], "failure text", "{}", succeeded=False, last_error="private error")
    repeat = repo.finish_server_ai_job(job["id"], "another failure text", "{}", succeeded=False)
    assert repeat["id"] == response["id"]
    assert len(event_rows(db)) == len(event_deliveries(db)) == 1
    assert event_rows(db)[0]["event_type"] == "ai.failed"
    assert "private error" not in event_rows(db)[0]["payload_json"]
    assert "failure text" not in event_rows(db)[0]["payload_json"]


def test_ai_failure_outbox_error_rolls_back_response_and_status(repo, db, seeded, monkeypatch):
    owner,project,_,_ = seeded
    job = make_ai_job(repo,owner,project)
    repo.claim_next_server_ai_job({"deepseek"})
    original = NotificationStore.capture_event

    def fail_after_event(self, **kwargs):
        original(self, **kwargs)
        raise RuntimeError("transaction interrupted")

    monkeypatch.setattr(NotificationStore, "capture_event", fail_after_event)
    before_entries = db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    with pytest.raises(RuntimeError):
        repo.finish_server_ai_job(job["id"], "failure text", "{}", succeeded=False)
    assert repo.get_server_ai_job(job["id"])["status"] == "running"
    assert repo.get_server_ai_job(job["id"])["response_entry_id"] is None
    assert db.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == before_entries
    assert event_rows(db) == [] and event_deliveries(db) == []


def test_daily_verification_limit_and_expired_tokens(repo, db, monkeypatch):
    owner = repo.create_owner_if_missing("rate-owner", "password")
    store = NotificationStore(db)
    tokens = []
    for minute in range(5):
        moment = f"2026-10-04T10:{minute*2:02}:00+00:00"
        monkeypatch.setattr("codex_hub.notifications.storage.utc_now", lambda value=moment: value)
        tokens.append(store.request_verification(owner["id"],"check@example.com",token_secret="secret")["token"])
    monkeypatch.setattr("codex_hub.notifications.storage.utc_now", lambda: "2026-10-04T10:10:00+00:00")
    with pytest.raises(ValueError, match="五次"):
        store.request_verification(owner["id"],"check@example.com",token_secret="secret")
    assert store.inspect_verification(tokens[0]) is None
    assert store.inspect_verification(tokens[-1]) is not None
    monkeypatch.setattr("codex_hub.notifications.storage.utc_now", lambda: "2026-10-04T10:39:00+00:00")
    assert store.inspect_verification(tokens[-1]) is None


def test_owner_scope_filters_delivery_and_attempt_queries(repo, db, seeded):
    owner,_,task,store = seeded
    repo.update_task_status(task["id"],"done")
    delivery = event_deliveries(db)[0]
    assert store.get_delivery(owner["id"]+1,delivery["id"]) is None
    assert store.list_deliveries(owner["id"]+1) == []
    assert store.list_attempts(owner["id"]+1,delivery["id"]) == []
    with pytest.raises(ValueError):
        store.set_recipient(owner["id"]+1,"other@example.com")


def test_resume_channel_keeps_old_events_cancelled_and_requires_new_enable(repo, db, seeded):
    owner,_,task,store = seeded
    repo.update_task_status(task["id"],"done")
    delivery = event_deliveries(db)[0]
    store.quarantine_restored_queue()
    store.resume_channel(owner["id"])
    assert store.get_channel_state()["paused_reason"] == ""
    assert not store.get_settings(owner["id"])["enabled"]
    assert store.get_delivery(owner["id"],delivery["id"])["status"] == "cancelled"


def test_deployment_disabled_events_cannot_replay_after_reenable(repo, db, seeded):
    owner,_,task,store = seeded
    assert store.get_settings(owner["id"])["enabled"]
    disabled_repo = Repository(db)
    disabled_repo.update_task_status(task["id"], "done")
    event = event_rows(db)[0]
    assert event["decision_reason"] == "service_disabled"
    assert json.loads(event["decision_json"])["deployment_enabled"] is False
    assert event_deliveries(db) == []
    repo.update_task_status(task["id"], "done")
    assert len(event_rows(db)) == 1 and event_deliveries(db) == []
    repo.update_task_status(task["id"], "open")
    repo.update_task_status(task["id"], "done")
    assert len(event_rows(db)) == 2 and len(event_deliveries(db)) == 1
    assert event_deliveries(db)[0]["event_id"] != event["id"]


def test_deployment_disabled_ai_failure_does_not_replay(repo, db, seeded):
    owner,project,_,store = seeded
    assert store.get_settings(owner["id"])["enabled"]
    job = make_ai_job(repo,owner,project)
    disabled_repo = Repository(db, email_notifications_enabled=False)
    disabled_repo.finish_server_ai_job(job["id"], "failure text", "{}", succeeded=False)
    assert event_rows(db)[0]["decision_reason"] == "service_disabled"
    assert event_deliveries(db) == []
    repo.finish_server_ai_job(job["id"], "failure text", "{}", succeeded=False)
    assert len(event_rows(db)) == 1 and event_deliveries(db) == []
    second_job = make_ai_job(repo,owner,project)
    repo.finish_server_ai_job(second_job["id"], "failure text", "{}", succeeded=False)
    assert len(event_rows(db)) == 2 and len(event_deliveries(db)) == 1


@pytest.mark.parametrize("deployment_enabled", [False, True])
def test_ai_runtime_propagates_deployment_gate(settings, repo, db, seeded, monkeypatch, deployment_enabled):
    from codex_hub.server_ai import ProviderRequestError, ServerAIRuntime

    owner,project,_,_ = seeded
    runtime_settings = replace(settings,email_enabled=deployment_enabled,server_ai_enabled=True,
                               deepseek_api_key="test-key-never-networked",server_ai_max_attempts=1)
    monkeypatch.setattr("codex_hub.server_ai.is_deepseek_peak_time",lambda _moment=None: True)

    class FailureClient:
        def complete(self, messages):
            raise ProviderRequestError("final failure",retryable=False)

    runtime = ServerAIRuntime(runtime_settings,client_factory=lambda *_args: FailureClient())
    runtime.prepare(repo)
    job = make_ai_job(repo,owner,project)
    assert runtime.run_once()
    assert repo.get_server_ai_job(job["id"])["status"] == "failed"
    assert len(event_rows(db)) == 1
    assert event_rows(db)[0]["decision_reason"] == ("eligible" if deployment_enabled else "service_disabled")
    assert len(event_deliveries(db)) == int(deployment_enabled)
