"""Mail credentials authorize queued sends, never Hub sessions or device access."""
import hashlib
import json
import re
from dataclasses import replace

import pytest

from codex_hub.mail.storage import MAX_CONTENT_BYTES, MAX_REQUEST_BYTES, MailStore
from codex_hub.notifications.storage import NotificationStore, utc_now
from codex_hub.security import hash_password


API = "/api/v1/mail/messages"
MANAGE = "/mail"
MESSAGE = {"to": "someone@example.com", "subject": "脚本提醒", "text": "Only the script supplied this body."}


@pytest.fixture(autouse=True)
def no_web_network_sends(monkeypatch):
    from codex_hub.notifications.provider import ResendProvider

    def forbidden(*args, **kwargs):
        raise AssertionError("Mail web routes must never make provider network calls")

    monkeypatch.setattr(ResendProvider, "send", forbidden)
    monkeypatch.setattr(ResendProvider, "lookup", forbidden)


@pytest.fixture
def owner(client, repo, settings):
    response = client.post("/login", data={"username": settings.owner_username, "password": settings.owner_password})
    assert response.status_code == 200
    return dict(repo.connection.execute("SELECT * FROM users WHERE role='owner'").fetchone())


@pytest.fixture
def ready(app, settings):
    app.state.settings = replace(settings, email_enabled=True, mail_api_enabled=True,
        email_api_key="unit-test-never-sent", email_webhook_secret="whsec_unit_test",
        email_from_address="Hub <notify@notify.example.test>", email_domain_verified=True,
        email_public_base_url="https://hub.example.test/hub")
    return app.state.settings


@pytest.fixture
def token(owner, db):
    return MailStore(db).create_token(owner["id"], "script-under-test")


def headers(token, key="unique-event-001"):
    return {"Authorization": "Bearer " + token["raw"], "Idempotency-Key": key}


def csrf(client, settings):
    return {"_csrf": client.cookies.get(settings.csrf_cookie_name)}


def create_from_ui(client, settings, **changes):
    return client.post(MANAGE + "/tokens", data={**csrf(client, settings), "name": "monitor",
        "hourly_limit": "10", "daily_limit": "50", **changes})


@pytest.mark.parametrize("method,path", [("get", API), ("get", API + "/missing"), ("post", API)])
def test_mail_api_rejects_cookie_only_even_for_owner(client, owner, method, path):
    response = getattr(client, method)(path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["cache-control"] == "no-store"


def test_device_and_mail_credential_namespaces_cannot_cross(client, repo, token):
    device = repo.create_device("test-device")
    device_token = repo.create_api_token(device["id"], "device-key")
    assert client.get(API, headers={"Authorization": "Bearer " + device_token.raw}).status_code == 401
    assert client.get("/api/projects", headers=headers(token)).status_code == 401
    client.cookies.clear()
    assert client.get(MANAGE, headers=headers(token), follow_redirects=False).headers["location"] == "/login"
    assert client.post(MANAGE + "/tokens", headers=headers(token), data={"name": "forged"}).status_code == 401


@pytest.mark.parametrize("authorization", ["", "Basic abc", "Bearer bad", "Bearer hmail_", "Bearer x extra", "Bearer  x"])
def test_invalid_authorization_is_uniform_and_does_not_reflect_input(client, authorization):
    response = client.get(API, headers={"Authorization": authorization})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"
    assert "Bearer bad" not in response.text


def test_api_queues_arbitrary_recipient_without_primary_binding_and_never_sends(client, token, ready, db):
    client.cookies.clear()
    response = client.post(API, headers=headers(token), json=MESSAGE)
    assert response.status_code == 202 and not response.json()["replayed"]
    delivery = response.json()["deliveries"][0]
    assert delivery["address"] == MESSAGE["to"] and delivery["status"] == "queued"
    assert db.execute("SELECT COUNT(*) FROM email_recipients").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM email_deliveries").fetchone()[0] == 0
    stored = db.execute("SELECT * FROM mail_deliveries").fetchone()
    assert stored["attempt_count"] == 0 and stored["provider_id"] is None
    payload = json.loads(stored["payload_json"])
    assert payload["from"] == ready.email_from_address and payload["to"] == [MESSAGE["to"]]
    assert response.headers["cache-control"] == "no-store"


def test_message_reads_are_token_scoped_and_exclude_private_fields(client, token, owner, ready, db):
    first = client.post(API, headers=headers(token), json=MESSAGE).json()
    other = MailStore(db).create_token(owner["id"], "other-application")
    second = client.post(API, headers=headers(other), json={**MESSAGE, "to": "other@example.net"}).json()
    assert client.get(API + "/" + first["id"], headers=headers(other)).status_code == 404
    assert client.get(API + "/" + second["id"], headers=headers(token)).status_code == 404
    listed = client.get(API, headers=headers(token))
    assert [item["id"] for item in listed.json()["messages"]] == [first["id"]]
    item = client.get(API + "/" + first["id"], headers=headers(token))
    assert set(item.json()) == {"id", "created_at", "deliveries"}
    assert set(item.json()["deliveries"][0]) == {"id", "address", "status", "created_at", "updated_at", "last_error"}
    for response in (listed, item):
        assert response.headers["cache-control"] == "no-store"
        for private in (MESSAGE["text"], token["raw"], "payload_json", "token_hash", "lease_token", "provider_id", "request_hash", "idempotency_key"):
            assert private not in response.text


def test_idempotency_replay_is_stable_and_conflicting_content_is_rejected(client, token, ready, db):
    first = client.post(API, headers=headers(token), json=MESSAGE)
    duplicate = client.post(API, headers=headers(token), json=MESSAGE)
    assert duplicate.status_code == 200 and duplicate.json()["replayed"]
    assert duplicate.json()["id"] == first.json()["id"]
    conflict = client.post(API, headers=headers(token), json={**MESSAGE, "subject": "different"})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"
    assert db.execute("SELECT COUNT(*) FROM mail_requests").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM mail_deliveries").fetchone()[0] == 1


@pytest.mark.parametrize("key", ["", "tiny", "with space", "x" * 129])
def test_invalid_or_missing_idempotency_key_never_queues(client, token, ready, db, key):
    response = client.post(API, headers=headers(token, key), json=MESSAGE)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_idempotency_key"
    assert db.execute("SELECT COUNT(*) FROM mail_requests").fetchone()[0] == 0


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", ""])
def test_content_type_must_be_json(client, token, ready, content_type):
    response = client.post(API, headers={**headers(token), "Content-Type": content_type}, content=json.dumps(MESSAGE))
    assert response.status_code == 415 and response.json()["error"]["code"] == "unsupported_media_type"


@pytest.mark.parametrize("body", [b"{", b"\xff"], ids=["malformed-json", "invalid-utf8"])
def test_malformed_utf8_or_json_is_bounded_and_never_queues(client, token, ready, db, body):
    response = client.post(API, headers={**headers(token), "Content-Type": "application/json"}, content=body)
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_json"
    assert db.execute("SELECT COUNT(*) FROM mail_requests").fetchone()[0] == 0


def test_deeply_nested_json_is_rejected_without_server_error(client, token, ready, db):
    response = client.post(API, headers={**headers(token), "Content-Type": "application/json"},
        content=b"[" * 1500 + b"]" * 1500)
    # JSON decoder recursion limits differ between supported Python versions.
    assert response.status_code in {400, 422}
    assert response.json()["error"]["code"] in {"invalid_json", "invalid_fields"}
    assert db.execute("SELECT COUNT(*) FROM mail_requests").fetchone()[0] == 0


@pytest.mark.parametrize("length,expected", [("nonsense", 400), ("-1", 413), (str(MAX_REQUEST_BYTES + 1), 413)])
def test_invalid_declared_content_length(client, token, ready, length, expected):
    response = client.post(API, headers={**headers(token), "Content-Type": "application/json", "Content-Length": length}, content=b"{}")
    assert response.status_code == expected


def test_oversized_stream_is_rejected_even_with_false_small_content_length(client, token, ready, db):
    response = client.post(API, headers={**headers(token), "Content-Type": "application/json", "Content-Length": "2"},
        content=b"x" * (MAX_REQUEST_BYTES + 1))
    assert response.status_code == 413 and response.json()["error"]["code"] == "content_too_large"
    assert db.execute("SELECT COUNT(*) FROM mail_requests").fetchone()[0] == 0


@pytest.mark.parametrize("payload,code", [
    ([], "invalid_fields"),
    ({**MESSAGE, "from": "attacker@example.com"}, "invalid_fields"),
    ({**MESSAGE, "cc": "other@example.net"}, "invalid_fields"),
    ({**MESSAGE, "subject": "Header\r\nBcc: private@example.net"}, "invalid_subject"),
    ({**MESSAGE, "to": []}, "invalid_recipients"),
    ({**MESSAGE, "to": ["one@example.com"] * 21}, "invalid_recipients"),
    ({**MESSAGE, "reply_to": "bad\n@example.com"}, "invalid_reply_to"),
    ({**MESSAGE, "text": None}, "invalid_content"),
    ({**MESSAGE, "text": "\ud800"}, "invalid_content"),
    ({"to": "one@example.com", "subject": "s"}, "content_required"),
])
def test_invalid_payloads_return_safe_errors(client, token, ready, db, payload, code):
    # Explicit ASCII encoding permits testing a JSON escaped lone surrogate.
    response = client.post(API, headers={**headers(token), "Content-Type": "application/json"}, content=json.dumps(payload).encode())
    assert response.status_code == 422 and response.json()["error"]["code"] == code
    assert db.execute("SELECT COUNT(*) FROM mail_requests").fetchone()[0] == 0


def test_content_limit_counts_combined_utf8_bytes(client, token, ready):
    response = client.post(API, headers=headers(token), json={**MESSAGE,
        "text": "a" * (MAX_CONTENT_BYTES // 2), "html": "b" * (MAX_CONTENT_BYTES // 2 + 1)})
    assert response.status_code == 413 and response.json()["error"]["code"] == "content_too_large"


@pytest.mark.parametrize("gate", ["api", "transport", "domain", "secret", "channel"])
def test_send_gates_do_not_block_readonly_status_or_mutate_queue(client, token, ready, db, gate):
    first = client.post(API, headers=headers(token), json=MESSAGE).json()
    if gate == "channel":
        db.execute("INSERT INTO email_channel_state(id,paused_reason,updated_at) VALUES(1,'paused',?)", (utc_now(),))
        db.commit()
    else:
        setattr(ready, {"api": "mail_api_enabled", "transport": "email_enabled", "domain": "email_domain_verified", "secret": "email_webhook_secret"}[gate], False if gate != "secret" else "")
    blocked = client.post(API, headers=headers(token, "another-event-002"), json=MESSAGE)
    assert blocked.status_code == 503 and blocked.json()["error"]["code"] == "service_unavailable"
    assert client.get(API + "/" + first["id"], headers=headers(token)).status_code == 200
    assert db.execute("SELECT COUNT(*) FROM mail_requests").fetchone()[0] == 1


def test_notification_recipient_and_rule_changes_do_not_change_application_mail(client, owner, token, ready, db):
    notifications = NotificationStore(db)
    notifications.set_recipient(owner["id"], "primary@example.net")
    notifications.update_settings(owner["id"], enabled=False, task_done=False, task_blocked=False, ai_failed=False)
    first = client.post(API, headers=headers(token), json=MESSAGE)
    assert first.status_code == 202
    notifications.revoke_recipient(owner["id"])
    second = client.post(API, headers=headers(token, "second-event-002"), json={**MESSAGE, "to": "third@example.org"})
    assert second.status_code == 202
    assert [row[0] for row in db.execute("SELECT status FROM mail_deliveries")] == ["queued", "queued"]


def test_management_default_is_not_ready_and_owner_only(client, owner, db):
    response = client.get(MANAGE)
    assert response.status_code == 200 and "发送服务暂停或未就绪" in response.text
    assert "无需绑定主邮箱" in response.text and "还没有发送记录" in response.text
    db.execute("UPDATE users SET role='viewer' WHERE id=?", (owner["id"],))
    db.commit()
    assert client.get(MANAGE).status_code == 403
    assert client.post(MANAGE + "/tokens", data={"name": "unauthorized"}).status_code == 403


@pytest.mark.parametrize("path", ["/tokens", "/tokens/fake/revoke", "/recipients/resume"])
def test_management_mutations_require_session_bound_csrf(client, owner, settings, path):
    for data in ({}, {"_csrf": "wrong"}):
        assert client.post(MANAGE + path, data=data).status_code == 403
    # A forged cookie alone cannot invent a valid double-submit token.
    client.cookies.set(settings.csrf_cookie_name, "forged", domain="testserver.local", path="/")
    assert client.post(MANAGE + path, data={"_csrf": "forged"}).status_code == 403


def test_token_secret_shown_only_at_creation_is_not_stored_and_pages_are_not_cached(client, owner, settings, db):
    response = create_from_ui(client, settings)
    assert response.status_code == 200
    raw = re.search(r'id="new-mail-key"[^>]*value="([^"]+)"', response.text).group(1)
    assert re.fullmatch(r"hmail_[A-Za-z0-9_-]{43}", raw)
    token = dict(db.execute("SELECT * FROM mail_api_tokens").fetchone())
    assert token["token_hash"] == hashlib.sha256(raw.encode()).hexdigest()
    assert raw not in repr(token)
    again = client.get(MANAGE)
    assert raw not in again.text and token["token_prefix"] in again.text
    assert token["token_hash"] not in again.text
    for page in (response, again):
        assert page.headers["cache-control"] == "no-store"
        assert page.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize("field,value", [("hourly_limit", "not-number"), ("daily_limit", "0"), ("daily_limit", "1001"), ("name", ""), ("name", "bad\nname")])
def test_management_invalid_token_fields_do_not_create_keys(client, owner, settings, db, field, value):
    response = create_from_ui(client, settings, **{field: value})
    assert response.status_code == 422
    assert db.execute("SELECT COUNT(*) FROM mail_api_tokens").fetchone()[0] == 0


def test_revocation_rejects_future_api_use_cancels_queue_and_preserves_other_tokens(client, owner, token, ready, settings, db):
    first = client.post(API, headers=headers(token), json=MESSAGE).json()
    other = MailStore(db).create_token(owner["id"], "another-script")
    response = client.post(MANAGE + "/tokens/" + token["id"] + "/revoke", data=csrf(client, settings), follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == MANAGE + "?notice=revoked"
    assert db.execute("SELECT status FROM mail_deliveries WHERE request_id=?", (first["id"],)).fetchone()[0] == "cancelled"
    assert client.get(API, headers=headers(token)).status_code == 401
    assert client.post(API, headers=headers(token), json=MESSAGE).status_code == 401
    assert client.get(API, headers=headers(other)).status_code == 200


def test_other_owner_cannot_revoke_token_or_read_its_requests(client, token, ready, settings, repo, db):
    client.post(API, headers=headers(token), json=MESSAGE)
    now = utc_now()
    other = db.execute("INSERT INTO users(username,password_hash,role,created_at,updated_at) VALUES(?,?,'owner',?,?)",
        ("other-owner", hash_password("test-password"), now, now)).lastrowid
    db.commit()
    session = repo.create_session(other)
    client.cookies.clear()
    client.cookies.set(settings.session_cookie_name, session.raw)
    client.cookies.set(settings.csrf_cookie_name, session.csrf)
    assert client.post(MANAGE + "/tokens/" + token["id"] + "/revoke", data=csrf(client, settings)).status_code == 404
    page = client.get(MANAGE)
    assert token["token_prefix"] not in page.text and MESSAGE["subject"] not in page.text


def test_resume_requires_explicit_confirmation_and_does_not_replay_old_mail(client, owner, token, ready, settings, db):
    first = client.post(API, headers=headers(token), json=MESSAGE).json()
    store = MailStore(db)
    with store.transaction():
        store.suppress_address(MESSAGE["to"], "hard_bounce")
    denied = client.post(API, headers=headers(token, "new-event-002"), json=MESSAGE)
    assert denied.status_code == 409 and denied.json()["error"]["code"] == "recipient_suppressed"
    data = {**csrf(client, settings), "address": MESSAGE["to"]}
    assert client.post(MANAGE + "/recipients/resume", data=data).status_code == 422
    assert store.is_suppressed(MESSAGE["to"])
    response = client.post(MANAGE + "/recipients/resume", data={**data, "cause_fixed": "on"}, follow_redirects=False)
    assert response.status_code == 303 and not store.is_suppressed(MESSAGE["to"])
    assert db.execute("SELECT status FROM mail_deliveries WHERE request_id=?", (first["id"],)).fetchone()[0] == "cancelled"
    assert client.post(API, headers=headers(token, "new-event-002"), json=MESSAGE).status_code == 202


def test_html_escaping_and_body_never_rendered_in_management(client, owner, token, ready, settings, db):
    attack = '<img src=x onerror="alert(1)">'
    create = create_from_ui(client, settings, name=attack)
    assert attack not in create.text and "&lt;img" in create.text
    response = client.post(API, headers=headers(token), json={**MESSAGE, "subject": attack, "html": "<script>private_body()</script>"})
    assert response.status_code == 202
    page = client.get(MANAGE + "?notice=" + attack)
    assert attack not in page.text and "&lt;img" in page.text
    assert "private_body" not in page.text and MESSAGE["text"] not in page.text
    assert ready.email_api_key not in page.text


def test_forwarded_prefix_applies_to_management_links_forms_and_redirects(client, owner, token, settings):
    page = client.get(MANAGE, headers={"X-Forwarded-Prefix": "/hub"})
    assert 'action="/hub/mail/tokens"' in page.text
    assert 'href="/hub/static/mail.css' in page.text
    assert 'action="/hub/mail/tokens/' + token["id"] + '/revoke"' in page.text
    response = client.post(MANAGE + "/tokens/" + token["id"] + "/revoke",
        headers={"X-Forwarded-Prefix": "/hub"}, data=csrf(client, settings), follow_redirects=False)
    assert response.headers["location"] == "/hub/mail?notice=revoked"


def test_quota_rejection_is_atomic_and_replay_does_not_consume_more(client, token, ready, db):
    ready.mail_api_hourly_limit = 1
    first = client.post(API, headers=headers(token), json=MESSAGE)
    assert first.status_code == 202
    assert client.post(API, headers=headers(token), json=MESSAGE).status_code == 200
    rejected = client.post(API, headers=headers(token, "another-event-002"), json=MESSAGE)
    assert rejected.status_code == 429 and rejected.json()["error"]["code"] == "quota_exceeded"
    assert int(rejected.headers["retry-after"]) >= 1
    assert db.execute("SELECT COUNT(*) FROM mail_requests").fetchone()[0] == 1
