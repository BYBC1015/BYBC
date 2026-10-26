from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from codex_hub.config import Settings
from codex_hub.db import connect_database, initialize_schema
from codex_hub.notifications.provider import (
    EMAIL_ENDPOINT, ProviderError, ProviderResult, ResendProvider,
    WebhookVerificationError, verify_webhook,
)
from codex_hub.notifications.storage import NotificationStore
from codex_hub.notifications.templates import render_email
from codex_hub.notifications.worker import NotificationWorker, TOKEN_PLACEHOLDER
from codex_hub.repository import Repository


class FakeProvider:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [ProviderResult("provider-123")])
        self.posts = []
        self.lookups = []

    def send(self, payload, key):
        self.posts.append((payload, key))
        value = self.outcomes.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def lookup(self, provider_id):
        self.lookups.append(provider_id)
        return ProviderResult(provider_id, "delivered")


@pytest.fixture
def rig(tmp_path):
    settings = Settings(data_dir=tmp_path / "state", secret_key="test-only-secret",
                        email_enabled=True, email_api_key="fake-never-networked",
                        email_webhook_secret="whsec_test", email_domain_verified=True,
                        email_public_base_url="https://hub.example.test/hub",
                        email_rate_interval_seconds=0)
    settings.ensure_directories()
    db = connect_database(settings.database_path)
    initialize_schema(db)
    repo = Repository(db)
    owner = repo.create_owner_if_missing("owner", "test-password")
    device = repo.create_device(label="test")
    project = repo.create_project(slug="private-project", name="私密项目 <内部>", created_by_device_id=device["id"])
    store = NotificationStore(db)
    recipient = store.set_recipient(owner["id"], "123456789@qq.com")
    db.execute("UPDATE email_recipients SET verified_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), recipient["id"]))
    db.commit()
    store.update_settings(owner["id"], enabled=True, show_project_name=False)
    clock = [datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=2)]

    def enqueue(event_type="task.done", entity_id="task-one"):
        db.execute("BEGIN IMMEDIATE")
        store.capture_event(event_type=event_type, entity_type="task", entity_id=entity_id,
                            status_version=1, project_id=project["id"], occurred_at=clock[0].isoformat())
        db.commit()
        return db.execute("SELECT * FROM email_deliveries ORDER BY rowid DESC LIMIT 1").fetchone()["id"]

    def row(delivery_id):
        return dict(db.execute("SELECT * FROM email_deliveries WHERE id=?", (delivery_id,)).fetchone())

    class Rig:
        pass
    result = Rig()
    result.settings, result.db, result.store, result.owner = settings, db, store, owner
    result.project, result.recipient, result.clock = project, recipient, clock
    result.enqueue, result.row = enqueue, row
    result.worker = lambda provider: NotificationWorker(settings, provider=provider, clock=lambda: clock[0], jitter=lambda: 0)
    yield result
    db.close()


def test_pinned_transport_and_idempotent_request_contract():
    requests = []
    def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"id": "p-123", "last_event": "delivered"})
        return httpx.Response(200, json={"id": "p-123"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = ResendProvider("provider-secret", client=client)
    assert provider.send({"to": ["123@qq.com"]}, "hub-email/opaque123").status == "accepted"
    assert provider.lookup("p-123").status == "delivered"
    assert str(requests[0].url) == EMAIL_ENDPOINT
    assert requests[0].headers["Idempotency-Key"] == "hub-email/opaque123"
    assert requests[1].method == "GET"
    assert "Idempotency-Key" not in requests[1].headers
    assert "provider-secret" not in repr(provider)
    with pytest.raises(ProviderError):
        provider.lookup("../../other-host")
    client.close()


@pytest.mark.parametrize("status,uncertain,retryable,pause", [(429,False,True,False), (500,True,True,False), (401,False,False,True), (403,False,False,True), (422,False,False,True)])
def test_provider_errors_are_safe_and_classified(status, uncertain, retryable, pause):
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, headers={"Retry-After":"180"}, json={"message":"secret-request-and-address"})))
    provider = ResendProvider("provider-secret", client=client)
    with pytest.raises(ProviderError) as caught:
        provider.send({"to":["private@qq.com"]}, "hub-email/opaque123")
    error = caught.value
    assert (error.uncertain,error.retryable,error.pause_channel) == (uncertain,retryable,pause)
    assert "secret" not in str(error) and "private" not in repr(error)
    if status == 429:
        assert error.retry_after == 180
    client.close()


@pytest.mark.parametrize("status", [401, 403])
def test_lookup_access_error_does_not_imply_sending_permission_error(status):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
            status, json={"message": "sensitive provider diagnostic"}))) as client:
        provider = ResendProvider("provider-secret", client=client)
        with pytest.raises(ProviderError) as caught:
            provider.lookup("provider-123")
        assert caught.value.code == "lookup_access_denied"
        assert not caught.value.pause_channel and not caught.value.pause_recipient
        assert caught.value.retry_after == 3600
        assert "sensitive" not in str(caught.value)
        with pytest.raises(ProviderError) as caught:
            provider.send({}, "hub-email-opaque123")
        assert caught.value.code == "credentials_or_domain_rejected"
        assert caught.value.pause_channel


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("observed", ["accepted", "delivery_delayed"])
def test_send_only_lookup_preserves_facts_new_sends_and_webhooks(rig, status, observed):
    requests = []
    posts = []

    def handler(request):
        requests.append(request.method)
        if request.method == "GET":
            return httpx.Response(status, json={"message": "sending-only key"})
        posts.append(request)
        return httpx.Response(200, json={"id": f"provider-{len(posts)}"})

    first = rig.enqueue()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        worker = rig.worker(ResendProvider("send-only-secret", client=client))
        assert worker.run_once()
        if observed == "delivery_delayed":
            rig.store.ingest_webhook("delay-before-lookup", "email.delivery_delayed",
                                     "provider-1", rig.clock[0].isoformat())
        rig.clock[0] += timedelta(minutes=10)
        assert worker.run_once()
        denied_at = rig.clock[0]
        stored = rig.row(first)
        assert stored["status"] == observed and stored["attempt_count"] == 1
        assert stored["provider_id"] == "provider-1"
        assert stored["last_error"] == "lookup_access_denied"
        assert datetime.fromisoformat(stored["next_attempt_at"]) == denied_at + timedelta(hours=1)
        assert rig.store.get_channel_state()["paused_reason"] == ""
        assert rig.store.get_recipient(rig.owner["id"])["paused_reason"] == ""
        for seconds in (1, 61, 3599):
            rig.clock[0] = denied_at + timedelta(seconds=seconds)
            assert not worker.run_once()
        # A process restart still respects the persisted delay and can send new mail.
        second = rig.enqueue(entity_id="second-after-denied-lookup")
        worker = rig.worker(ResendProvider("send-only-secret", client=client))
        assert worker.run_once()
        assert rig.row(second)["status"] == "accepted"
        assert requests == ["POST", "GET", "POST"]
        rig.clock[0] = denied_at + timedelta(hours=1)
        assert worker.run_once()
        assert requests == ["POST", "GET", "POST", "GET"]
        assert rig.row(first)["status"] == observed
        # Valid provider callbacks continue to reconcile even while GET is denied.
        rig.store.ingest_webhook("delivered-after-denied-lookup", "email.delivered",
                                 "provider-1", rig.clock[0].isoformat())
        assert rig.row(first)["status"] == "delivered"
        assert rig.store.get_channel_state()["paused_reason"] == ""


@pytest.mark.parametrize("status", [401, 403, 422])
def test_post_permission_or_request_rejection_still_pauses_new_sends(rig, status):
    requests = []

    def handler(request):
        requests.append(request.method)
        return httpx.Response(status, json={"message": "rejected"})

    first = rig.enqueue()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        worker = rig.worker(ResendProvider("invalid-sender-key", client=client))
        assert worker.run_once()
        assert rig.row(first)["status"] == "failed"
        assert rig.store.get_channel_state()["paused_reason"]
        second = rig.enqueue(entity_id="after-rejected-post")
        rig.clock[0] += timedelta(minutes=2)
        assert not worker.run_once()
        assert rig.row(second)["status"] == "cancelled"
        assert requests == ["POST"]


@pytest.mark.parametrize("error", [
    ProviderError("network_uncertain", uncertain=True, retryable=True),
    ProviderError("request_rejected", pause_channel=True, pause_recipient=True),
])
def test_lookup_errors_cannot_pause_sending_or_poll_every_minute(rig, error):
    class LookupFailure(FakeProvider):
        def lookup(self, provider_id):
            self.lookups.append(provider_id)
            raise error

    first = rig.enqueue()
    provider = LookupFailure()
    worker = rig.worker(provider)
    assert worker.run_once()
    rig.clock[0] += timedelta(minutes=10)
    assert worker.run_once()
    assert rig.row(first)["status"] == "accepted"
    assert rig.store.get_channel_state()["paused_reason"] == ""
    assert rig.store.get_recipient(rig.owner["id"])["paused_reason"] == ""
    rig.clock[0] += timedelta(minutes=9)
    assert not worker.run_once()
    assert provider.lookups == ["provider-123"]
    rig.clock[0] += timedelta(minutes=1)
    assert worker.run_once()
    assert provider.lookups == ["provider-123", "provider-123"]


def test_transport_timeout_is_uncertain_and_redirects_are_not_followed():
    def timeout(request):
        raise httpx.ReadTimeout("body and credentials must not escape", request=request)
    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(ProviderError, match="network_uncertain") as caught:
            ResendProvider("secret", client=client).send({}, "hub-email/opaque123")
        assert caught.value.uncertain
    requests = []
    def redirect(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location":"https://evil.example"})
    with httpx.Client(transport=httpx.MockTransport(redirect), follow_redirects=True) as client:
        with pytest.raises(ProviderError):
            ResendProvider("secret", client=client).send({}, "hub-email/opaque123")
    assert len(requests) == 1


def test_processing_idempotent_request_is_retryable_but_changed_payload_is_permanent():
    for name, retryable in (("concurrent_idempotent_requests", True), ("invalid_idempotent_request", False)):
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(409,json={"name":name,"message":"sensitive"}))) as client:
            with pytest.raises(ProviderError) as caught:
                ResendProvider("secret",client=client).send({},"hub-email-opaque123")
        assert caught.value.retryable is retryable
        assert caught.value.uncertain is retryable
        assert caught.value.pause_channel is not retryable


def test_svix_raw_body_signature_rotation_timestamp_and_malformed_json():
    body = b'{"type":"email.delivered", "data":{"email_id":"p-123"}}'
    secret_bytes = b"test-webhook-signing-key"
    secret = "whsec_" + base64.b64encode(secret_bytes).decode()
    timestamp = "1770000000"
    signature = base64.b64encode(hmac.new(secret_bytes, b"msg-123." + timestamp.encode() + b"." + body, hashlib.sha256).digest()).decode()
    headers = {"svix-id":"msg-123", "svix-timestamp":timestamp, "svix-signature":"v1,old-invalid v1,"+signature}
    assert verify_webhook(body,headers,secret,now=int(timestamp))["type"] == "email.delivered"
    for altered, changed, at in [(body.replace(b" ",b""),headers,int(timestamp)), (body,{**headers,"svix-id":"other"},int(timestamp)), (body,headers,int(timestamp)+301), (body,headers,int(timestamp)-301)]:
        with pytest.raises(WebhookVerificationError):
            verify_webhook(altered,changed,secret,now=at)
    with pytest.raises(WebhookVerificationError):
        verify_webhook(body,headers,"invalid-key",now=int(timestamp))


def test_templates_hide_names_everywhere_escape_html_and_use_opaque_links():
    args = dict(public_base_url="https://hub.example.test/prefix",delivery_id="opaque123",occurred_at="2026-10-04T06:30:00+00:00",project_name="Secret <img src=x>\r\nBcc:bad")
    hidden = render_email("task.blocked",**args)
    assert "Secret" not in json.dumps(hidden) and "Bcc:" not in json.dumps(hidden)
    assert "https://hub.example.test/prefix/notifications/email/open/opaque123" in hidden["text"]
    assert "2026-10-04 14:30" in hidden["text"]
    shown = render_email("task.done",show_project_name=True,**args)
    assert "\n" not in shown["subject"] and "\r" not in shown["subject"]
    assert "<img" not in shown["html"] and "&lt;img" in shown["html"]
    assert "<img" in shown["text"]


def test_worker_acceptance_is_not_delivery_and_payload_is_private(rig):
    delivery = rig.enqueue()
    provider = FakeProvider()
    assert rig.worker(provider).run_once()
    stored = rig.row(delivery)
    assert stored["status"] == "accepted"
    assert stored["provider_id"] == "provider-123"
    assert stored["attempt_count"] == 1 and stored["payload_hash"]
    assert "私密" not in stored["payload_json"] and "private-project" not in stored["payload_json"]
    assert stored["address"] == "123456789@qq.com"
    assert rig.db.execute("SELECT outcome FROM email_delivery_attempts").fetchone()[0] == "accepted"


def test_retry_after_and_frozen_payload_key_across_restart(rig):
    delivery = rig.enqueue()
    first = FakeProvider([ProviderError("rate_limited",retryable=True,retry_after=180)])
    assert rig.worker(first).run_once()
    before = rig.row(delivery)
    assert before["status"] == "retry_wait" and not before["uncertain_submission"]
    rig.clock[0] += timedelta(seconds=179)
    second = FakeProvider()
    assert not rig.worker(second).run_once()
    rig.clock[0] += timedelta(seconds=1)
    rig.settings.email_from_address = "changed@example.test"
    assert rig.worker(second).run_once()
    assert second.posts == first.posts
    after = rig.row(delivery)
    assert after["payload_json"] == before["payload_json"]
    assert after["first_attempt_at"] == before["first_attempt_at"]


def test_crashed_post_reuses_payload_and_key_after_lease(rig):
    delivery = rig.enqueue()
    original = rig.worker(FakeProvider())
    claim = original._claim()
    assert claim and rig.row(delivery)["status"] == "sending"
    rig.clock[0] += timedelta(seconds=121)
    resumed = FakeProvider()
    assert rig.worker(resumed).run_once()
    assert resumed.posts == [(claim["payload"],claim["idempotency_key"])]
    assert rig.row(delivery)["attempt_count"] == 2
    # A late, fenced-out process cannot downgrade the new worker's response.
    original._finish(claim,None,ProviderError("credentials_or_domain_rejected",pause_channel=True))
    assert rig.row(delivery)["status"] == "accepted"
    assert rig.db.execute("SELECT paused_reason FROM email_channel_state").fetchone()[0] == ""


def test_two_workers_only_one_can_lease_same_message(rig):
    delivery = rig.enqueue()
    first = rig.worker(FakeProvider())._claim()
    assert first
    provider = FakeProvider()
    assert not rig.worker(provider).run_once()
    assert not provider.posts and rig.row(delivery)["attempt_count"] == 1


def test_uncertain_window_never_changes_key_or_posts_again(rig):
    delivery = rig.enqueue()
    initial = FakeProvider([ProviderError("network_uncertain",retryable=True,uncertain=True)])
    rig.worker(initial).run_once()
    key = rig.row(delivery)["idempotency_key"]
    rig.clock[0] += timedelta(hours=23)
    later = FakeProvider()
    assert not rig.worker(later).run_once()
    assert not later.posts
    assert rig.row(delivery)["status"] == "uncertain"
    assert rig.row(delivery)["idempotency_key"] == key


@pytest.mark.parametrize("action",["master","rule","recipient","restore"])
def test_disabling_unknown_submission_prevents_further_post(rig,action):
    delivery = rig.enqueue()
    rig.worker(FakeProvider([ProviderError("network_uncertain",retryable=True,uncertain=True)])).run_once()
    if action == "master":
        rig.settings.email_enabled = False
    elif action == "rule":
        rig.store.update_settings(rig.owner["id"], enabled=True, task_done=False)
    elif action == "recipient":
        rig.store.revoke_recipient(rig.owner["id"])
    else:
        rig.store.quarantine_restored_queue()
    rig.clock[0] += timedelta(minutes=10)
    provider = FakeProvider()
    rig.worker(provider).run_once()
    assert not provider.posts and rig.row(delivery)["status"] == "uncertain"


def test_revoked_known_submission_only_queries_and_preserves_delivery_fact(rig):
    delivery = rig.enqueue()
    provider = FakeProvider()
    worker = rig.worker(provider)
    worker.run_once()
    rig.store.revoke_recipient(rig.owner["id"])
    rig.settings.email_enabled = False
    rig.clock[0] += timedelta(minutes=11)
    assert worker.run_once()
    assert len(provider.posts) == 1 and provider.lookups == ["provider-123"]
    assert rig.row(delivery)["status"] == "delivered"


def test_default_disabled_never_makes_network_request(rig):
    delivery = rig.enqueue()
    rig.settings.email_enabled = False
    provider = FakeProvider()
    assert not rig.worker(provider).run_once()
    assert not provider.posts and not provider.lookups
    assert rig.row(delivery)["status"] == "cancelled"


def test_webhook_before_response_and_stale_callback_do_not_regress(rig):
    delivery = rig.enqueue()
    class EarlyWebhook(FakeProvider):
        def send(self,payload,key):
            other = connect_database(rig.settings.database_path)
            try:
                NotificationStore(other).ingest_webhook("webhook-delivered","email.delivered","provider-123",rig.clock[0].isoformat())
            finally:
                other.close()
            return super().send(payload,key)
    assert rig.worker(EarlyWebhook()).run_once()
    assert rig.row(delivery)["status"] == "delivered"
    rig.store.ingest_webhook("webhook-sent","email.sent","provider-123",(rig.clock[0]-timedelta(seconds=5)).isoformat())
    assert rig.row(delivery)["status"] == "delivered"
    assert not rig.store.ingest_webhook("webhook-sent","email.sent","provider-123",rig.clock[0].isoformat())


def test_permanent_failure_pauses_channel_and_does_not_leak_message(rig):
    delivery = rig.enqueue()
    provider = FakeProvider([ProviderError("credentials_or_domain_rejected",pause_channel=True)])
    rig.worker(provider).run_once()
    assert rig.row(delivery)["status"] == "failed"
    other = rig.enqueue(entity_id="task-two")
    rig.clock[0] += timedelta(minutes=2)
    assert not rig.worker(provider).run_once()
    assert len(provider.posts) == 1 and rig.row(other)["status"] == "cancelled"


def test_quota_only_counts_first_submission_and_expiry_is_recorded(rig):
    rig.settings.email_hourly_limit = 1
    first = rig.enqueue(entity_id="first")
    second = rig.enqueue(entity_id="second")
    provider = FakeProvider()
    worker = rig.worker(provider)
    worker.run_once()
    assert not worker.run_once()
    assert len(provider.posts) == 1
    assert rig.row(second)["last_error"] == "quota_wait"
    rig.clock[0] += timedelta(hours=25)
    # First message has a provider ID and may be queried, but the waiting one expires.
    worker.run_once()
    worker.run_once()
    assert rig.row(second)["status"] == "expired"
    assert len(provider.posts) == 1


def test_verification_materializes_token_only_in_memory_and_respects_rotation(rig):
    request = rig.store.request_verification(rig.owner["id"],"other@qq.com",token_secret=rig.settings.secret_key)
    provider = FakeProvider([ProviderError("network_uncertain",retryable=True,uncertain=True)])
    rig.worker(provider).run_once()
    delivery = rig.row(request["delivery_id"])
    assert request["token"] in provider.posts[0][0]["text"]
    assert request["token"] not in delivery["payload_json"]
    assert TOKEN_PLACEHOLDER in delivery["payload_json"]
    rig.settings.secret_key = "rotated-secret"
    rig.clock[0] += timedelta(minutes=2)
    later = FakeProvider()
    rig.worker(later).run_once()
    assert not later.posts
    assert rig.row(request["delivery_id"])["status"] == "uncertain"


def test_six_known_temporary_failures_stop_without_creating_new_delivery(rig):
    delivery = rig.enqueue()
    provider = FakeProvider([ProviderError("rate_limited",retryable=True) for _ in range(6)])
    worker = rig.worker(provider)
    for _ in range(6):
        assert worker.run_once()
        rig.clock[0] = datetime.fromisoformat(rig.row(delivery)["next_attempt_at"])
    assert len(provider.posts) == 6
    assert len({key for _,key in provider.posts}) == 1
    assert rig.row(delivery)["status"] == "failed"
    assert not worker.run_once()


def test_restored_unknown_cannot_post_after_channel_and_rule_reenabled(rig):
    delivery = rig.enqueue()
    rig.worker(FakeProvider())._claim()
    rig.store.quarantine_restored_queue()
    rig.store.resume_channel(rig.owner["id"])
    rig.store.update_settings(rig.owner["id"], enabled=True)
    rig.clock[0] += timedelta(minutes=5)
    provider = FakeProvider()
    rig.worker(provider).run_once()
    assert not provider.posts
    assert rig.row(delivery)["status"] == "uncertain"
    assert rig.row(delivery)["last_error"] in {"backup_restored", "channel_resumed_requires_opt_in"}


def test_demoted_owner_cannot_send_queued_notifications(rig):
    delivery = rig.enqueue()
    rig.db.execute("UPDATE users SET role='member' WHERE id=?", (rig.owner["id"],))
    rig.db.commit()
    provider = FakeProvider()
    rig.worker(provider).run_once()
    assert not provider.posts and rig.row(delivery)["status"] == "cancelled"


def test_stale_lookup_cannot_downgrade_observed_delivery_delay(rig):
    delivery = rig.enqueue()
    class StaleLookup(FakeProvider):
        def lookup(self,provider_id):
            return ProviderResult(provider_id,"accepted")
    worker = rig.worker(StaleLookup())
    worker.run_once()
    rig.store.ingest_webhook("delay-event","email.delivery_delayed","provider-123",rig.clock[0].isoformat())
    rig.clock[0] += timedelta(minutes=11)
    worker.run_once()
    assert rig.row(delivery)["status"] == "delivery_delayed"


@pytest.mark.parametrize("action", ["revoke", "disable"])
def test_inflight_post_acceptance_survives_cancellation_and_associates_callback(rig, action):
    delivery = rig.enqueue()
    class ConcurrentCancellation(FakeProvider):
        def send(self, payload, key):
            if action == "revoke":
                rig.store.revoke_recipient(rig.owner["id"])
            else:
                rig.store.update_settings(rig.owner["id"], enabled=False)
            rig.store.ingest_webhook("early-delivered", "email.delivered", "provider-123", rig.clock[0].isoformat())
            return super().send(payload,key)
    provider = ConcurrentCancellation()
    worker = rig.worker(provider)
    worker.run_once()
    assert rig.row(delivery)["status"] == "delivered"
    assert rig.row(delivery)["provider_id"] == "provider-123"
    assert rig.row(delivery)["lease_token"] is None
    assert rig.db.execute("SELECT outcome FROM email_delivery_attempts").fetchone()[0] == "accepted"
    rig.clock[0] += timedelta(hours=1)
    worker.run_once()
    assert len(provider.posts) == 1


def test_late_acceptance_after_revocation_enables_readonly_lookup(rig):
    delivery = rig.enqueue()
    worker = rig.worker(FakeProvider())
    claim = worker._claim()
    rig.store.revoke_recipient(rig.owner["id"])
    worker._finish(claim,ProviderResult("provider-123"),None)
    assert rig.row(delivery)["status"] == "accepted"
    assert rig.row(delivery)["provider_id"] == "provider-123"
    worker.run_once()
    assert worker.provider.lookups == ["provider-123"]
    assert not worker.provider.posts


def test_lookup_complaint_upgrades_concurrent_delivered_callback(rig):
    delivery = rig.enqueue()
    class ComplaintLookup(FakeProvider):
        def lookup(self, provider_id):
            rig.store.ingest_webhook("delivered-during-get","email.delivered",provider_id,rig.clock[0].isoformat())
            return ProviderResult(provider_id,"complained")
    worker = rig.worker(ComplaintLookup())
    worker.run_once()
    rig.clock[0] += timedelta(minutes=11)
    worker.run_once()
    assert rig.row(delivery)["status"] == "complained"
    assert rig.store.get_recipient(rig.owner["id"])["paused_reason"] == "complained"


def test_lookup_error_preserves_concurrent_delay_callback(rig):
    delivery = rig.enqueue()
    class FailingLookup(FakeProvider):
        def lookup(self, provider_id):
            rig.store.ingest_webhook("delay-during-get","email.delivery_delayed",provider_id,rig.clock[0].isoformat())
            raise ProviderError("network_uncertain",uncertain=True,retryable=True)
    worker = rig.worker(FailingLookup())
    worker.run_once()
    rig.clock[0] += timedelta(minutes=11)
    worker.run_once()
    assert rig.row(delivery)["status"] == "delivery_delayed"


def test_verification_marker_in_recipient_never_changes_address(rig):
    address = TOKEN_PLACEHOLDER + "@example.com"
    request = rig.store.request_verification(rig.owner["id"],address,token_secret=rig.settings.secret_key)
    provider = FakeProvider()
    rig.worker(provider).run_once()
    assert provider.posts[0][0]["to"] == [address]
    assert request["token"] in provider.posts[0][0]["text"]
