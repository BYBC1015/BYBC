"""Administrator quantity exemptions do not bypass delivery protections."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

import pytest

from codex_hub.config import Settings
from codex_hub.db import connect_database, initialize_schema
from codex_hub.mail.storage import MailStore
from codex_hub.mail.worker import MailWorker
from codex_hub.notifications.provider import ProviderError, ProviderResult
from codex_hub.notifications.storage import NotificationStore
from codex_hub.notifications.worker import NotificationWorker
from codex_hub.repository import Repository


class FakeProvider:
    def __init__(self, outcomes=None):
        self.posts = []
        self.outcomes = list(outcomes or [])

    def send(self, payload, key):
        self.posts.append((payload, key))
        outcome = self.outcomes.pop(0) if self.outcomes else ProviderResult(f"admin-provider-{len(self.posts)}")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def lookup(self, provider_id):
        return ProviderResult(provider_id, "delivered")


@pytest.fixture
def admin_rig(tmp_path):
    settings = Settings(data_dir=tmp_path / "state", secret_key="isolated-admin-worker-test",
        email_enabled=True, mail_api_enabled=True, email_api_key="never-networked",
        email_webhook_secret="whsec_test", email_domain_verified=True,
        email_public_base_url="https://hub.example.test/hub", email_rate_interval_seconds=0,
        mail_api_hourly_limit=1, mail_api_daily_limit=1)
    settings.ensure_directories()
    db = connect_database(settings.database_path)
    initialize_schema(db)
    owner = Repository(db).create_owner_if_missing("owner", "test-password")
    store = MailStore(db)
    admin = store.create_token(owner["id"], "Administrator", hourly_limit=1, daily_limit=1, quota_exempt=True)
    limited = store.create_token(owner["id"], "Limited", hourly_limit=100, daily_limit=100)
    clock = [datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=2)]

    def enqueue(token, address="target@example.com"):
        result = store.enqueue(token["id"], {"to": address, "subject": "Condition met", "text": "Test only"},
            "test-event-" + uuid.uuid4().hex, from_address=settings.email_from_address,
            hourly_limit=100, daily_limit=100)
        return result["deliveries"][0]["id"]

    def row(delivery):
        return dict(db.execute("SELECT * FROM mail_deliveries WHERE id=?", (delivery,)).fetchone())

    yield SimpleNamespace(settings=settings, db=db, store=store, owner=owner, admin=admin, limited=limited,
        clock=clock, enqueue=enqueue, row=row,
        worker=lambda provider: MailWorker(settings, provider=provider, clock=lambda: clock[0], jitter=lambda: 0))
    db.close()


@pytest.mark.parametrize("gate", ["mail_api_hourly_limit", "mail_api_daily_limit"])
def test_exempt_queue_sends_above_quantity_cap_without_numeric_override(admin_rig, gate):
    rig = admin_rig
    rig.settings.mail_api_hourly_limit = rig.settings.mail_api_daily_limit = 100
    setattr(rig.settings, gate, 1)
    deliveries = [rig.enqueue(rig.admin) for _ in range(3)]
    provider = FakeProvider()
    worker = rig.worker(provider)
    assert all(worker.run_once() for _ in deliveries)
    assert [rig.row(item)["status"] for item in deliveries] == ["accepted"] * 3
    assert len(provider.posts) == 3
    token = rig.db.execute("SELECT hourly_limit,daily_limit,quota_exempt FROM mail_api_tokens WHERE id=?", (rig.admin["id"],)).fetchone()
    assert tuple(token) == (1, 1, 1)


def test_normal_queue_still_waits_while_exempt_mail_bypasses_full_pool(admin_rig):
    rig = admin_rig
    first, waiting = rig.enqueue(rig.limited), rig.enqueue(rig.limited)
    exempt = rig.enqueue(rig.admin)
    provider = FakeProvider()
    worker = rig.worker(provider)
    assert worker.run_once() and worker.run_once()
    assert rig.row(first)["status"] == rig.row(exempt)["status"] == "accepted"
    assert rig.row(waiting)["status"] == "queued"
    assert rig.row(waiting)["last_error"] == "quota_wait"
    assert rig.row(waiting)["attempt_count"] == 0
    assert len(provider.posts) == 2


def test_admin_sends_do_not_consume_normal_token_global_pool(admin_rig):
    rig = admin_rig
    admin_mail = [rig.enqueue(rig.admin) for _ in range(3)]
    normal_first, normal_second = rig.enqueue(rig.limited), rig.enqueue(rig.limited)
    provider = FakeProvider()
    worker = rig.worker(provider)
    assert all(worker.run_once() for _ in range(4))
    assert not worker.run_once()
    assert all(rig.row(item)["status"] == "accepted" for item in [*admin_mail, normal_first])
    assert rig.row(normal_second)["last_error"] == "quota_wait"
    assert len(provider.posts) == 4


@pytest.mark.parametrize("claimed", [False, True])
def test_revoked_admin_token_cannot_send_pending_mail(admin_rig, claimed):
    rig = admin_rig
    delivery = rig.enqueue(rig.admin)
    provider = FakeProvider()
    worker = rig.worker(provider)
    claim = worker._claim() if claimed else None
    rig.store.revoke_token(rig.owner["id"], rig.admin["id"])
    if claim:
        assert not worker._still_allowed(claim)
    else:
        assert not worker.run_once()
    assert rig.row(delivery)["status"] == ("uncertain" if claimed else "cancelled")
    assert not provider.posts


def test_admin_mail_keeps_shared_provider_pacing(admin_rig):
    rig = admin_rig
    rig.settings.email_rate_interval_seconds = 2
    first, second = rig.enqueue(rig.admin), rig.enqueue(rig.admin)
    provider = FakeProvider()
    worker = rig.worker(provider)
    assert worker.run_once()
    assert not worker.run_once()
    assert rig.row(second)["status"] == "queued"
    rig.clock[0] += timedelta(seconds=2)
    assert worker.run_once()
    assert rig.row(first)["status"] == rig.row(second)["status"] == "accepted"


def test_notification_request_reserves_same_provider_slot_as_admin_mail(admin_rig):
    rig = admin_rig
    rig.settings.email_rate_interval_seconds = 2
    NotificationStore(rig.db).request_verification(rig.owner["id"], "primary@example.com", token_secret=rig.settings.secret_key)
    delivery = rig.enqueue(rig.admin)
    provider = FakeProvider()
    notifications = NotificationWorker(rig.settings, provider=provider, clock=lambda: rig.clock[0], jitter=lambda: 0)
    assert notifications.run_once()
    assert not rig.worker(provider).run_once()
    assert rig.row(delivery)["status"] == "queued"
    rig.clock[0] += timedelta(seconds=2)
    assert rig.worker(provider).run_once()
    assert rig.row(delivery)["status"] == "accepted" and len(provider.posts) == 2


@pytest.mark.parametrize("gate", ["owner", "suppression", "channel", "service"])
def test_admin_exemption_does_not_bypass_protections(admin_rig, gate):
    rig = admin_rig
    delivery = rig.enqueue(rig.admin)
    if gate == "owner":
        rig.db.execute("UPDATE users SET role='member' WHERE id=?", (rig.owner["id"],))
    elif gate == "suppression":
        with rig.store.transaction():
            rig.store.suppress_address(rig.row(delivery)["address"], "hard_bounce")
    elif gate == "channel":
        rig.db.execute("INSERT INTO email_channel_state(id,paused_reason,updated_at) VALUES(1,'provider_pause',?)", (rig.clock[0].isoformat(),))
    else:
        rig.settings.email_enabled = False
    rig.db.commit()
    provider = FakeProvider()
    assert not rig.worker(provider).run_once()
    assert rig.row(delivery)["status"] == "cancelled" and not provider.posts


def test_admin_retry_keeps_frozen_payload_key_and_twenty_three_hour_fence(admin_rig):
    rig = admin_rig
    delivery = rig.enqueue(rig.admin)
    provider = FakeProvider([ProviderError("network_uncertain", retryable=True, uncertain=True),
                             ProviderError("network_uncertain", retryable=True, uncertain=True)])
    worker = rig.worker(provider)
    assert worker.run_once()
    rig.settings.email_from_address = "Changed <other@example.com>"
    rig.clock[0] += timedelta(minutes=1)
    assert worker.run_once()
    assert provider.posts[0] == provider.posts[1]
    rig.clock[0] += timedelta(hours=23)
    assert not worker.run_once()
    assert rig.row(delivery)["status"] == "uncertain" and len(provider.posts) == 2


def test_admin_exemption_does_not_allow_tampered_payload(admin_rig):
    rig = admin_rig
    delivery = rig.enqueue(rig.admin)
    rig.db.execute("UPDATE mail_deliveries SET payload_hash='tampered' WHERE id=?", (delivery,))
    rig.db.commit()
    provider = FakeProvider()
    assert not rig.worker(provider).run_once()
    assert rig.row(delivery)["status"] == "failed"
    assert rig.row(delivery)["last_error"] == "immutable_payload_invalid" and not provider.posts
