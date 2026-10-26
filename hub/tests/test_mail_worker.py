from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import httpx

from codex_hub.config import Settings
from codex_hub.db import connect_database, initialize_schema
from codex_hub.mail.schema import initialize_mail_schema
from codex_hub.mail.storage import MailStore
from codex_hub.mail.worker import MailWorker
from codex_hub.notifications.provider import ProviderError, ProviderResult, ResendProvider
from codex_hub.notifications.worker import _digest
from codex_hub.repository import Repository


class FakeProvider:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.posts = []
        self.lookups = []
        self.closed = False

    def send(self, payload, key):
        self.posts.append((payload, key))
        value = self.outcomes.pop(0) if self.outcomes else ProviderResult(f"provider-{len(self.posts)}")
        if isinstance(value, BaseException):
            raise value
        return value

    def lookup(self, provider_id):
        self.lookups.append(provider_id)
        return ProviderResult(provider_id, "delivered")

    def close(self):
        self.closed = True


@pytest.fixture
def rig(tmp_path):
    settings = Settings(data_dir=tmp_path / "state", secret_key="test-only-secret",
                        email_enabled=True, email_api_key="fake-never-networked",
                        email_webhook_secret="whsec_test", email_domain_verified=True,
                        email_public_base_url="https://hub.example.test/hub",
                        email_rate_interval_seconds=0, mail_api_enabled=True)
    settings.ensure_directories()
    db = connect_database(settings.database_path)
    initialize_schema(db)
    initialize_mail_schema(db)
    owner = Repository(db).create_owner_if_missing("owner", "test-password")
    clock = [datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=2)]
    token_id = uuid.uuid4().hex
    db.execute("""INSERT INTO mail_api_tokens
        (id,owner_id,name,token_prefix,token_hash,hourly_limit,daily_limit,created_at)
        VALUES(?,?,?,?,?,?,?,?)""",
        (token_id, owner["id"], "Unit test", "mail-test", "test-hash", 100, 1000, clock[0].isoformat()))
    db.commit()

    def enqueue(address="different@example.com", *, subject="脚本提醒", reply_to="reply@example.com"):
        request_id, delivery_id = uuid.uuid4().hex, uuid.uuid4().hex
        stamp = clock[0].isoformat()
        payload = {"from": settings.email_from_address, "to": [address], "subject": subject,
                   "text": "Threshold reached", "html": "<p>Threshold reached</p>",
                   "reply_to": reply_to,
                   "tags": [{"name": "hub_mail_delivery", "value": delivery_id}]}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        db.execute("""INSERT INTO mail_requests
            (id,token_id,owner_id,idempotency_key,request_hash,payload_json,created_at)
            VALUES(?,?,?,?,?,?,?)""",
            (request_id, token_id, owner["id"], uuid.uuid4().hex, _digest(payload), encoded, stamp))
        db.execute("""INSERT INTO mail_deliveries
            (id,request_id,token_id,owner_id,address,payload_json,payload_hash,idempotency_key,
             next_attempt_at,expires_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (delivery_id, request_id, token_id, owner["id"], address, encoded, _digest(payload),
             f"hub-mail/{delivery_id}", stamp, (clock[0] + timedelta(hours=24)).isoformat(), stamp, stamp))
        db.commit()
        return delivery_id

    def row(delivery_id):
        return dict(db.execute("SELECT * FROM mail_deliveries WHERE id=?", (delivery_id,)).fetchone())

    result = SimpleNamespace(settings=settings, db=db, store=MailStore(db), owner=owner,
                             token_id=token_id, clock=clock, enqueue=enqueue, row=row)
    result.worker = lambda provider: MailWorker(settings, provider=provider, clock=lambda: clock[0], jitter=lambda: 0)
    yield result
    db.close()


def test_arbitrary_recipient_sends_without_notification_recipient_or_enabled_rule(rig):
    delivery = rig.enqueue()
    assert rig.db.execute("SELECT COUNT(*) FROM email_recipients").fetchone()[0] == 0
    assert rig.db.execute("SELECT COUNT(*) FROM email_notification_settings WHERE enabled=1").fetchone()[0] == 0
    provider = FakeProvider()
    worker = rig.worker(provider)
    assert worker.run_once()
    stored = rig.row(delivery)
    assert stored["status"] == "accepted" and stored["provider_id"] == "provider-1"
    assert stored["attempt_count"] == 1
    payload = provider.posts[0][0]
    assert payload["to"] == ["different@example.com"]
    assert payload["subject"] == "脚本提醒" and payload["reply_to"] == "reply@example.com"
    assert rig.db.execute("SELECT outcome FROM mail_delivery_attempts").fetchone()[0] == "accepted"
    worker.close()
    assert provider.closed


def test_network_call_holds_no_sqlite_write_transaction(rig):
    rig.enqueue()

    class ConcurrentWrite(FakeProvider):
        def send(self, payload, key):
            other = connect_database(rig.settings.database_path)
            try:
                other.execute("PRAGMA busy_timeout=1")
                other.execute("BEGIN IMMEDIATE")
                other.execute("UPDATE mail_api_tokens SET last_used_at=?", (rig.clock[0].isoformat(),))
                other.commit()
            finally:
                other.close()
            return super().send(payload, key)

    assert rig.worker(ConcurrentWrite()).run_once()


def test_real_store_multi_recipient_payloads_are_independent_and_private(rig):
    token = rig.store.create_token(rig.owner["id"], "multi-recipient-test")
    request = rig.store.enqueue(token["id"], {
        "to": ["first@example.com", "second@example.net"], "subject": "条件触发",
        "text": "The condition matched.", "reply_to": "reply@example.org",
    }, "multi-recipient-once", from_address=rig.settings.email_from_address,
       hourly_limit=rig.settings.mail_api_hourly_limit, daily_limit=rig.settings.mail_api_daily_limit)
    provider = FakeProvider()
    worker = rig.worker(provider)
    assert worker.run_once() and worker.run_once()
    assert len(request["deliveries"]) == 2 and len(provider.posts) == 2
    assert {tuple(payload["to"]) for payload, _ in provider.posts} == {
        ("first@example.com",), ("second@example.net",),
    }
    assert len({key for _, key in provider.posts}) == 2
    assert all(payload["reply_to"] == "reply@example.org" for payload, _ in provider.posts)


def test_retry_after_preserves_payload_and_key_across_restart_and_sender_change(rig):
    delivery = rig.enqueue()
    provider = FakeProvider([ProviderError("rate_limited", retryable=True, retry_after=180)])
    assert rig.worker(provider).run_once()
    original = rig.row(delivery)
    assert original["status"] == "retry_wait" and not original["uncertain_submission"]
    rig.settings.email_from_address = "Changed <changed@example.com>"
    rig.clock[0] += timedelta(seconds=179)
    later = FakeProvider()
    assert not rig.worker(later).run_once()
    rig.clock[0] += timedelta(seconds=1)
    assert rig.worker(later).run_once()
    assert provider.posts == later.posts
    assert rig.row(delivery)["first_attempt_at"] == original["first_attempt_at"]


def test_crashed_worker_reuses_same_request_and_stale_error_cannot_pause_channel(rig):
    delivery = rig.enqueue()
    first = rig.worker(FakeProvider())
    claim = first._claim()
    assert claim
    rig.clock[0] += timedelta(seconds=121)
    provider = FakeProvider()
    assert rig.worker(provider).run_once()
    assert provider.posts == [(claim["payload"], claim["idempotency_key"])]
    assert rig.row(delivery)["attempt_count"] == 2
    first._finish(claim, None, ProviderError("credentials_or_domain_rejected", pause_channel=True))
    assert rig.row(delivery)["status"] == "accepted"
    assert rig.db.execute("SELECT paused_reason FROM email_channel_state").fetchone()[0] == ""


def test_two_workers_cannot_lease_one_delivery(rig):
    delivery = rig.enqueue()
    assert rig.worker(FakeProvider())._claim()
    provider = FakeProvider()
    assert not rig.worker(provider).run_once()
    assert not provider.posts and rig.row(delivery)["attempt_count"] == 1


def test_shared_provider_interval_blocks_even_a_new_worker(rig):
    rig.settings.email_rate_interval_seconds = 2
    first, second = rig.enqueue(), rig.enqueue("other@example.com")
    provider = FakeProvider()
    assert rig.worker(provider).run_once()
    assert not rig.worker(provider).run_once()
    assert rig.row(second)["status"] == "queued"
    rig.clock[0] += timedelta(seconds=2)
    assert rig.worker(provider).run_once()
    assert rig.row(first)["status"] == rig.row(second)["status"] == "accepted"


@pytest.mark.parametrize("uncertain", [False, True])
def test_twenty_three_hour_idempotency_deadline_never_creates_new_key(rig, uncertain):
    delivery = rig.enqueue()
    provider = FakeProvider([ProviderError("temporary", retryable=True, uncertain=uncertain)])
    rig.worker(provider).run_once()
    key = rig.row(delivery)["idempotency_key"]
    rig.clock[0] += timedelta(hours=23)
    assert not rig.worker(provider).run_once()
    assert len(provider.posts) == 1
    assert rig.row(delivery)["status"] == ("uncertain" if uncertain else "failed")
    assert rig.row(delivery)["idempotency_key"] == key


@pytest.mark.parametrize("uncertain", [False, True])
def test_six_attempts_are_bounded_and_preserve_idempotency(rig, uncertain):
    delivery = rig.enqueue()
    provider = FakeProvider([ProviderError("temporary", retryable=True, uncertain=uncertain) for _ in range(6)])
    worker = rig.worker(provider)
    for _ in range(6):
        assert worker.run_once()
        rig.clock[0] = datetime.fromisoformat(rig.row(delivery)["next_attempt_at"])
    assert not worker.run_once()
    assert len(provider.posts) == 6 and len({key for _, key in provider.posts}) == 1
    assert rig.row(delivery)["status"] == ("uncertain" if uncertain else "failed")


@pytest.mark.parametrize("change", ["token", "owner", "service", "api", "suppression", "channel"])
def test_eligibility_rechecked_immediately_before_post(rig, change):
    delivery = rig.enqueue()
    worker = rig.worker(FakeProvider())
    claim = worker._claim()
    if change == "token":
        rig.db.execute("UPDATE mail_api_tokens SET revoked_at=?", (rig.clock[0].isoformat(),))
    elif change == "owner":
        rig.db.execute("UPDATE users SET role='member'")
    elif change == "service":
        rig.settings.email_enabled = False
    elif change == "api":
        rig.settings.mail_api_enabled = False
    elif change == "suppression":
        rig.db.execute("""INSERT INTO mail_suppressions(address,reason,created_at,updated_at)
            VALUES(?,?,?,?)""", (rig.row(delivery)["address"], "hard_bounce", rig.clock[0].isoformat(), rig.clock[0].isoformat()))
    else:
        rig.db.execute("UPDATE email_channel_state SET paused_reason='test_pause'")
    rig.db.commit()
    assert not worker._still_allowed(claim)
    assert rig.row(delivery)["status"] == "cancelled"
    assert not worker.provider.posts


def test_revocation_stops_unknown_retry_but_known_acceptance_only_queries(rig):
    uncertain = rig.enqueue()
    known = rig.enqueue("known@example.com")
    provider = FakeProvider([ProviderError("network_uncertain", retryable=True, uncertain=True), ProviderResult("known-provider")])
    worker = rig.worker(provider)
    assert worker.run_once() and worker.run_once()
    rig.db.execute("UPDATE mail_api_tokens SET revoked_at=?", (rig.clock[0].isoformat(),))
    rig.db.commit()
    rig.settings.mail_api_enabled = False
    rig.clock[0] += timedelta(minutes=11)
    assert worker.run_once()
    assert rig.row(uncertain)["status"] == "uncertain"
    assert rig.row(known)["status"] == "delivered"
    assert len(provider.posts) == 2 and provider.lookups == ["known-provider"]


def test_restored_unknown_never_sends_after_service_resume(rig):
    delivery = rig.enqueue()
    assert rig.worker(FakeProvider())._claim()
    rig.store.quarantine_restored_queue()
    rig.db.execute("UPDATE email_channel_state SET paused_reason=''")
    rig.db.execute("UPDATE mail_api_tokens SET revoked_at=NULL")
    rig.db.commit()
    rig.clock[0] += timedelta(hours=2)
    provider = FakeProvider()
    assert not rig.worker(provider).run_once()
    assert not provider.posts and rig.row(delivery)["status"] == "uncertain"


def test_early_duplicate_and_out_of_order_webhooks_merge_after_post(rig):
    delivery = rig.enqueue()

    class EarlyCallback(FakeProvider):
        def send(self, payload, key):
            rig.store.ingest_webhook("delivered-before-post", "email.delivered", "provider-1", rig.clock[0].isoformat())
            return super().send(payload, key)

    assert rig.worker(EarlyCallback()).run_once()
    assert rig.row(delivery)["status"] == "delivered"
    assert not rig.store.ingest_webhook("delivered-before-post", "email.delivered", "provider-1", rig.clock[0].isoformat())
    rig.store.ingest_webhook("stale-sent", "email.sent", "provider-1", (rig.clock[0] - timedelta(seconds=10)).isoformat())
    assert rig.row(delivery)["status"] == "delivered"


@pytest.mark.parametrize("observed", ["accepted", "delivery_delayed"])
def test_sending_only_lookup_does_not_pause_new_mail_or_regress_facts(rig, observed):
    class DeniedLookup(FakeProvider):
        def lookup(self, provider_id):
            self.lookups.append(provider_id)
            raise ProviderError("lookup_access_denied", retry_after=3600, pause_channel=True, pause_recipient=True)

    delivery = rig.enqueue()
    provider = DeniedLookup()
    worker = rig.worker(provider)
    worker.run_once()
    if observed == "delivery_delayed":
        rig.store.ingest_webhook("delay", "email.delivery_delayed", "provider-1", rig.clock[0].isoformat())
    rig.clock[0] += timedelta(minutes=10)
    assert worker.run_once()
    assert rig.row(delivery)["status"] == observed
    assert rig.row(delivery)["attempt_count"] == 1
    assert datetime.fromisoformat(rig.row(delivery)["next_attempt_at"]) == rig.clock[0] + timedelta(hours=1)
    assert rig.db.execute("SELECT paused_reason FROM email_channel_state").fetchone()[0] == ""
    assert rig.db.execute("SELECT COUNT(*) FROM mail_suppressions").fetchone()[0] == 0
    second = rig.enqueue("second@example.com")
    assert worker.run_once() and rig.row(second)["status"] == "accepted"
    rig.store.ingest_webhook("delivered", "email.delivered", "provider-1", rig.clock[0].isoformat())
    assert rig.row(delivery)["status"] == "delivered"


def test_late_acceptance_after_revocation_associates_early_delivery_callback(rig):
    delivery = rig.enqueue()
    worker = rig.worker(FakeProvider())
    claim = worker._claim()
    rig.db.execute("UPDATE mail_api_tokens SET revoked_at=?", (rig.clock[0].isoformat(),))
    rig.db.execute("UPDATE mail_deliveries SET status='uncertain',lease_token=NULL,lease_expires_at=NULL")
    rig.db.commit()
    rig.store.ingest_webhook("late-delivered", "email.delivered", "provider-1", rig.clock[0].isoformat())
    worker._finish(claim, ProviderResult("provider-1"), None)
    assert rig.row(delivery)["status"] == "delivered"
    assert rig.row(delivery)["provider_id"] == "provider-1"
    assert rig.db.execute("SELECT outcome FROM mail_delivery_attempts").fetchone()[0] == "accepted"
    rig.clock[0] += timedelta(hours=1)
    assert not worker.run_once() and not worker.provider.posts


def test_late_acceptance_after_lease_expiry_is_preserved_when_lookup_is_denied(rig):
    delivery = rig.enqueue()

    class DeniedLookup(FakeProvider):
        def lookup(self, provider_id):
            self.lookups.append(provider_id)
            raise ProviderError("lookup_access_denied", retry_after=3600)

    provider = DeniedLookup()
    worker = rig.worker(provider)
    claim = worker._claim()
    rig.clock[0] += timedelta(seconds=121)
    worker._finish(claim, ProviderResult("provider-1"), None)
    assert rig.row(delivery)["status"] == "accepted"
    assert worker.run_once()
    assert rig.row(delivery)["status"] == "accepted"
    assert provider.lookups == ["provider-1"] and not provider.posts


def test_late_conflicting_provider_id_cannot_replace_associated_request(rig):
    delivery = rig.enqueue()
    worker = rig.worker(FakeProvider())
    old = worker._claim()
    rig.clock[0] += timedelta(seconds=121)
    worker.run_once()
    worker._finish(old, ProviderResult("wrong-provider"), None)
    assert rig.row(delivery)["provider_id"] == "provider-1"
    assert rig.row(delivery)["status"] == "accepted"


@pytest.mark.parametrize("result_status", ["bounced", "complained", "suppressed"])
def test_lookup_suppression_keeps_current_fact_and_cancels_other_queued_address(rig, result_status):
    first, second = rig.enqueue(), rig.enqueue()

    class SuppressedLookup(FakeProvider):
        def lookup(self, provider_id):
            return ProviderResult(provider_id, result_status)

    provider = SuppressedLookup()
    worker = rig.worker(provider)
    worker.run_once()
    # Hold a second queued message long enough to receive feedback for the first.
    rig.db.execute("UPDATE mail_deliveries SET next_attempt_at=? WHERE id=?",
                   ((rig.clock[0] + timedelta(hours=1)).isoformat(), second))
    rig.db.commit()
    rig.clock[0] += timedelta(minutes=10)
    assert worker.run_once()
    assert rig.row(first)["status"] == result_status
    assert rig.row(second)["status"] == "cancelled"
    assert rig.db.execute("SELECT reason FROM mail_suppressions").fetchone()[0] == result_status
    assert len(provider.posts) == 1


def test_permanent_send_error_pauses_shared_channel(rig):
    first, second = rig.enqueue(), rig.enqueue("second@example.com")
    provider = FakeProvider([ProviderError("credentials_or_domain_rejected", pause_channel=True)])
    worker = rig.worker(provider)
    worker.run_once()
    assert rig.row(first)["status"] == "failed"
    assert not worker.run_once() and rig.row(second)["status"] == "cancelled"
    assert len(provider.posts) == 1


@pytest.mark.parametrize("rejected_status", [400, 409, 422])
def test_provider_content_rejection_does_not_pause_other_messages(rig, rejected_status):
    first, second = rig.enqueue(), rig.enqueue("second@example.com")
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(rejected_status, json={"name": "validation_error", "message": "private content diagnostic"})
        return httpx.Response(200, json={"id": "provider-good"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        worker = rig.worker(ResendProvider("test-no-real-network", client=client))
        assert worker.run_once()
        assert rig.row(first)["status"] == "failed"
        assert rig.row(first)["last_error"] == "request_rejected"
        assert rig.db.execute("SELECT paused_reason FROM email_channel_state").fetchone()[0] == ""
        assert worker.run_once()
    assert rig.row(second)["status"] == "accepted"
    assert len(requests) == 2


@pytest.mark.parametrize("limit_field", ["mail_api_hourly_limit", "mail_api_daily_limit"])
def test_send_time_quota_is_global_across_addresses_and_waiting_mail_expires(rig, limit_field):
    setattr(rig.settings, limit_field, 1)
    first, second = rig.enqueue(), rig.enqueue("second@example.com")
    provider = FakeProvider()
    worker = rig.worker(provider)
    worker.run_once()
    assert not worker.run_once()
    assert rig.row(second)["last_error"] == "quota_wait"
    assert rig.row(second)["attempt_count"] == 0
    rig.clock[0] += timedelta(hours=25)
    worker.run_once()
    worker.run_once()
    assert rig.row(first)["status"] == "delivered"
    assert rig.row(second)["status"] == "expired" and len(provider.posts) == 1


@pytest.mark.parametrize("tamper", ["hash", "recipient", "json"])
def test_tampered_payload_cannot_be_sent(rig, tamper):
    delivery = rig.enqueue()
    if tamper == "hash":
        rig.db.execute("UPDATE mail_deliveries SET payload_hash='different'")
    elif tamper == "recipient":
        rig.db.execute("UPDATE mail_deliveries SET address='redirected@example.com'")
    else:
        rig.db.execute("UPDATE mail_deliveries SET payload_json='invalid-json'")
    rig.db.commit()
    provider = FakeProvider()
    assert not rig.worker(provider).run_once()
    assert not provider.posts and rig.row(delivery)["status"] == "failed"
    assert rig.row(delivery)["last_error"] == "immutable_payload_invalid"
