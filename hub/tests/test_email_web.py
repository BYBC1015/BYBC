"""Notification controls must stay session-bound and never send from a web request."""
import base64
import hashlib
import hmac
import json
import time
from dataclasses import replace

import pytest

from codex_hub.notifications.storage import NotificationStore, utc_now
from codex_hub.notifications.web import MAX_WEBHOOK_BYTES
from codex_hub.repository import Repository
from codex_hub.security import hash_password


PATH = "/settings/notifications/email"
WEBHOOK = "/api/webhooks/email/resend"
WEBHOOK_KEY = b"test-email-webhook-key-no-external-service"


@pytest.fixture
def email_owner(client, app, repo, settings, monkeypatch):
    from codex_hub.notifications.provider import ResendProvider

    def forbid_send(*args, **kwargs):
        raise AssertionError("A web route must only queue mail")

    monkeypatch.setattr(ResendProvider, "send", forbid_send)
    response = client.post("/login", data={"username": settings.owner_username, "password": settings.owner_password})
    assert response.status_code == 200
    owner = dict(repo.connection.execute("SELECT * FROM users WHERE role='owner' LIMIT 1").fetchone())
    return owner


@pytest.fixture
def email_ready(app, settings):
    app.state.settings = replace(settings, email_enabled=True,
        email_api_key="test-api-key-do-not-send", email_webhook_secret="whsec_" + base64.b64encode(WEBHOOK_KEY).decode(),
        email_from_address="Hub <notify@notify.example.test>",
        email_public_base_url="https://hub.example.test/hub", email_domain_verified=True)
    return app.state.settings


def csrf(client, settings):
    return {"_csrf": client.cookies.get(settings.csrf_cookie_name)}


def verified(db, owner, settings):
    store = NotificationStore(db)
    value = store.request_verification(owner["id"], "123456@example.test", token_secret=settings.secret_key)
    assert store.confirm_verification(value["token"])
    return value


def webhook_headers(body, *, event_id="wh-test-1", timestamp=None):
    timestamp = str(timestamp if timestamp is not None else int(time.time()))
    message = event_id.encode() + b"." + timestamp.encode() + b"." + body
    signature = base64.b64encode(hmac.new(WEBHOOK_KEY, message, hashlib.sha256).digest()).decode()
    return {"svix-id": event_id, "svix-timestamp": timestamp, "svix-signature": "v1," + signature,
            "content-type": "application/json"}


def webhook_body(**overrides):
    return json.dumps({"type": "email.delivered", "created_at": "2026-10-04T06:30:00Z",
                       "data": {"email_id": "provider-not-yet-associated"}, **overrides}).encode()


def test_default_page_has_no_fake_history_or_enabled_controls(client, email_owner, db):
    response = client.get(PATH)
    assert response.status_code == 200
    assert "尚未配置完成发送服务" in response.text
    assert 'name="enabled" disabled' in response.text
    assert 'placeholder="你的 QQ 邮箱或其他邮箱"' in response.text
    assert "test-api-key" not in response.text
    history = client.get(PATH + "?tab=history")
    assert "还没有发送记录" in history.text
    assert db.execute("SELECT COUNT(*) FROM email_deliveries").fetchone()[0] == 0


def test_management_requires_owner_session_not_device_token(client, repo, settings):
    device = repo.create_device("email-device")
    token = repo.create_api_token(device["id"], "email-device")
    headers = {"Authorization": "Bearer " + token.raw}
    assert client.post(PATH + "/recipient", headers=headers, data={"address": "123@qq.com"}).status_code == 401
    assert client.get(PATH, headers=headers, follow_redirects=False).headers["location"] == "/login"


def test_non_owner_cannot_view_or_mutate(client, repo, email_owner, settings):
    repo.connection.execute("UPDATE users SET role='viewer' WHERE id=?", (email_owner["id"],))
    repo.connection.commit()
    assert client.get(PATH).status_code == 403
    assert client.post(PATH + "/recipient", data={**csrf(client, settings), "address": "123@qq.com"}).status_code == 403
    assert "邮件通知</a>" not in client.get("/projects").text


@pytest.mark.parametrize("action", ["recipient", "rules", "verify", "test", "recipient/remove", "recipient/resume", "channel/resume"])
def test_all_management_mutations_require_session_bound_csrf(client, email_owner, settings, action):
    assert client.post(PATH + "/" + action, data={"_csrf": "wrong", "address": "123@qq.com"}).status_code == 403


def test_not_ready_can_save_address_and_preferences_but_cannot_send_or_enable(client, email_owner, db, settings):
    data = csrf(client, settings)
    response = client.post(PATH + "/recipient", data={**data, "address": "123456@QQ.com"}, follow_redirects=False)
    assert response.status_code == 303
    store = NotificationStore(db)
    assert store.get_recipient(email_owner["id"])["address"] == "123456@qq.com"
    assert client.post(PATH + "/rules", data={**data, "task_done": "on"}, follow_redirects=False).status_code == 303
    prefs = store.get_settings(email_owner["id"])
    assert prefs["task_done"] and not prefs["task_blocked"] and not prefs["show_project_name"]
    for action in ("verify", "test", "rules"):
        assert client.post(PATH + "/" + action, data={**data, "enabled": "on"}).status_code == 409
    assert db.execute("SELECT COUNT(*) FROM email_deliveries").fetchone()[0] == 0


def test_ready_still_requires_recipient_verification_for_test_and_enable(client, email_owner, email_ready, db, settings):
    NotificationStore(db).set_recipient(email_owner["id"], "123456@qq.com")
    for action in ("test", "rules"):
        assert client.post(PATH + "/" + action, data={**csrf(client, settings), "enabled": "on"}).status_code == 409
    assert db.execute("SELECT COUNT(*) FROM email_deliveries").fetchone()[0] == 0


def test_verification_request_only_queues_saved_address_with_rate_limit(client, email_owner, email_ready, db, settings):
    store = NotificationStore(db)
    store.set_recipient(email_owner["id"], "123456@qq.com")
    data = {**csrf(client, settings), "address": "attacker@example.test", "body": "arbitrary"}
    response = client.post(PATH + "/verify", data=data, follow_redirects=False)
    assert response.status_code == 303
    deliveries = store.list_deliveries(email_owner["id"])
    assert len(deliveries) == 1 and deliveries[0]["intent"] == "verification"
    assert deliveries[0]["address"] == "123456@qq.com" and deliveries[0]["status"] == "queued"
    assert deliveries[0]["payload_json"] is None
    assert client.post(PATH + "/verify", data=data).status_code == 429


def test_verification_get_never_consumes_token_and_post_needs_csrf(client, email_owner, email_ready, db, settings):
    store = NotificationStore(db)
    created = store.request_verification(email_owner["id"], "123456@qq.com", token_secret=settings.secret_key)
    path = "/notifications/email/verify?token=" + created["token"]
    for _ in range(2):
        page = client.get(path)
        assert page.status_code == 200 and "确认验证此邮箱" in page.text
        assert store.inspect_verification(created["token"]) is not None
        assert not store.get_recipient(email_owner["id"])["verified_at"]
    assert client.post("/notifications/email/verify", data={"token": created["token"]}).status_code == 403
    response = client.post("/notifications/email/verify", data={**csrf(client, settings), "token": created["token"]}, follow_redirects=False)
    assert response.status_code == 303
    assert store.get_recipient(email_owner["id"])["verified_at"]
    assert store.inspect_verification(created["token"]) is None
    assert client.post("/notifications/email/verify", data={**csrf(client, settings), "token": created["token"]}).status_code == 400


def test_verification_scanner_and_other_owner_cannot_confirm(client, email_owner, db, settings, repo):
    store = NotificationStore(db)
    created = store.request_verification(email_owner["id"], "123456@qq.com", token_secret=settings.secret_key)
    client.cookies.clear()
    page = client.get("/notifications/email/verify?token=" + created["token"])
    assert page.status_code == 401 and created["token"] not in page.text
    assert store.inspect_verification(created["token"])
    now = utc_now()
    cursor = db.execute("INSERT INTO users(username,password_hash,role,created_at,updated_at) VALUES(?,?,'owner',?,?)", ("other-owner", hash_password("password"), now, now))
    db.commit()
    # Sign in through the real route so the new browser session has one
    # correctly scoped CSRF cookie, including any session renewal response.
    client.cookies.clear()
    assert client.post('/login', data={'username': 'other-owner', 'password': 'password'}).status_code == 200
    assert client.get("/notifications/email/verify?token=" + created["token"]).status_code == 400
    assert client.post("/notifications/email/verify", data={**csrf(client, settings), "token": created["token"]}).status_code == 400
    assert store.inspect_verification(created["token"])
    assert client.get(PATH + "/deliveries/" + created["delivery_id"]).status_code == 404
    assert client.get("/notifications/email/open/" + created["delivery_id"]).status_code == 404


def test_verified_test_is_queued_while_auto_notifications_remain_off(client, email_owner, email_ready, db, settings):
    verified(db, email_owner, settings)
    response = client.post(PATH + "/test", data=csrf(client, settings), follow_redirects=False)
    assert response.status_code == 303
    store = NotificationStore(db)
    tests = [d for d in store.list_deliveries(email_owner["id"]) if d["intent"] == "test"]
    assert len(tests) == 1 and tests[0]["status"] == "queued"
    assert not store.get_settings(email_owner["id"])["enabled"]
    history = client.get(PATH + "?tab=history")
    assert "1***@example.test" in history.text and "123456@example.test" not in history.text
    assert "等待发送" in history.text
    assert client.get(PATH + "/deliveries/" + tests[0]["id"]).status_code == 200


def test_forwarded_prefix_is_preserved_in_forms_and_redirects(client, email_owner, settings):
    headers = {"X-Forwarded-Prefix": "/hub"}
    page = client.get(PATH, headers=headers)
    assert 'action="/hub/settings/notifications/email/recipient"' in page.text
    assert 'href="/hub/static/email-notifications.css' in page.text
    assert 'href="/hub/settings/notifications/email?tab=history"' in page.text
    response = client.post(PATH + "/recipient", headers=headers,
        data={**csrf(client, settings), "address": "123456@qq.com"}, follow_redirects=False)
    assert response.headers["location"].startswith("/hub/settings/notifications/email")


def test_preview_is_fixed_sandboxed_and_obeys_saved_privacy(client, email_owner, db):
    page = client.get(PATH + '?tab=preview&event=<script>alert(1)</script>&project_name=stolen')
    assert page.status_code == 200
    assert 'sandbox=""' in page.text and "固定模板预览" in page.text
    assert "示例项目" in page.text and "stolen" not in page.text
    assert "<script>alert(1)</script>" not in page.text
    NotificationStore(db).update_settings(email_owner["id"], enabled=False, show_project_name=False)
    private = client.get(PATH + "?tab=preview")
    assert "示例项目" not in private.text
    assert db.execute("SELECT COUNT(*) FROM email_deliveries").fetchone()[0] == 0


def test_stored_recipient_is_html_escaped(client, email_owner, db, settings):
    value = verified(db, email_owner, settings)
    malicious = '\"><img src=x onerror=alert(1)>@example.test'
    db.execute("UPDATE email_deliveries SET address=? WHERE id=?", (malicious, value["delivery_id"]))
    db.commit()
    page = client.get(PATH + "/deliveries/" + value["delivery_id"])
    assert page.status_code == 200
    assert '<img src=x onerror=alert(1)>' not in page.text
    assert '&lt;img src=x onerror=alert(1)&gt;' in page.text


def test_get_does_not_send_verification_or_test(client, email_owner, db):
    for action in ("verify", "test"):
        assert client.get(PATH + "/" + action).status_code == 405
    assert db.execute("SELECT COUNT(*) FROM email_deliveries").fetchone()[0] == 0


def test_webhook_requires_config_signature_freshness_and_size_cap(client, app, email_ready, db):
    body = webhook_body()
    assert client.post(WEBHOOK, content=body).status_code == 400
    headers = webhook_headers(body)
    assert client.post(WEBHOOK, content=body + b" ", headers=headers).status_code == 400
    assert client.post(WEBHOOK, content=body, headers=webhook_headers(body, timestamp=int(time.time()) - 1000)).status_code == 400
    assert client.post(WEBHOOK, content=b"x" * (MAX_WEBHOOK_BYTES + 1), headers=headers).status_code == 413
    assert db.execute("SELECT COUNT(*) FROM email_webhook_receipts").fetchone()[0] == 0
    app.state.settings = replace(email_ready, email_webhook_secret="")
    assert client.post(WEBHOOK, content=body, headers=headers).status_code == 503


def test_signed_webhook_persists_deduplicated_receipt_without_creating_task(client, email_ready, db):
    body = webhook_body()
    for _ in range(2):
        assert client.post(WEBHOOK, content=body, headers=webhook_headers(body)).json() == {"ok": True}
    assert db.execute("SELECT COUNT(*) FROM email_webhook_receipts").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM email_deliveries").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_invalid_signed_webhook_structure_is_rejected(client, email_ready, db):
    for body in (webhook_body(data={}), webhook_body(created_at="bad-date"), webhook_body(type="x" * 81), b"[]"):
        assert client.post(WEBHOOK, content=body, headers=webhook_headers(body)).status_code == 400
    assert db.execute("SELECT COUNT(*) FROM email_webhook_receipts").fetchone()[0] == 0


def test_opaque_link_resolves_only_owner_delivery_and_correct_task(client, email_owner, email_ready, db, settings):
    repo = Repository(db, email_notifications_enabled=email_ready.email_enabled)
    verified(db, email_owner, settings)
    store = NotificationStore(db)
    store.update_settings(email_owner["id"], enabled=True)
    project = repo.create_project("email-link-project", "Private name")
    task = repo.create_task(project["id"], "Do not email this title")
    repo.update_task_status(task["id"], "done")
    item = next(d for d in store.list_deliveries(email_owner["id"]) if d["event_type"] == "task.done")
    response = client.get("/notifications/email/open/" + item["id"], headers={"X-Forwarded-Prefix": "/hub"}, follow_redirects=False)
    assert response.headers["location"] == f"/hub/projects/email-link-project/tasks/{task['id']}"
    assert client.get("/notifications/email/open/unknown-notification").status_code == 404


def test_channel_pause_visible_and_resume_requires_explicit_confirmation(client, email_owner, email_ready, db, settings):
    now = utc_now()
    db.execute("INSERT INTO email_channel_state(id,paused_reason,updated_at) VALUES(1,'backup_restored',?)", (now,))
    db.commit()
    page = client.get(PATH)
    assert "已恢复历史备份" in page.text and "恢复发送通道" in page.text
    assert client.post(PATH + "/channel/resume", data=csrf(client, settings)).status_code == 422
    response = client.post(PATH + "/channel/resume", data={**csrf(client, settings), "cause_fixed": "on"}, follow_redirects=False)
    assert response.status_code == 303
    assert not NotificationStore(db).get_channel_state()["paused_reason"]
    assert not NotificationStore(db).get_settings(email_owner["id"])["enabled"]
