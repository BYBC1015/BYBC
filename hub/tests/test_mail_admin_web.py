"""Only an authenticated owner can issue a quota-exempt sending credential."""
import re
from dataclasses import replace

import pytest

from codex_hub.mail.storage import MailStore


@pytest.fixture
def admin(client, repo, settings, app):
    assert client.post('/login', data={'username': settings.owner_username,
                                      'password': settings.owner_password}).status_code == 200
    user = dict(repo.connection.execute("SELECT * FROM users WHERE role='owner'").fetchone())
    app.state.settings = replace(settings, email_enabled=True, mail_api_enabled=True,
        email_api_key='test-not-networked', email_webhook_secret='test-not-networked',
        email_from_address='Hub <notify@example.test>', email_domain_verified=True,
        email_public_base_url='https://hub.example.test/hub',
        mail_api_hourly_limit=1, mail_api_daily_limit=1)
    return user


def create(client, settings, **changes):
    return client.post('/mail/tokens', data={'_csrf': client.cookies.get(settings.csrf_cookie_name),
        'name': 'Creator mail', 'quota_exempt': 'on', 'hourly_limit': '1', 'daily_limit': '1', **changes})


def test_owner_creates_unlimited_key_once_and_page_labels_it(client, settings, admin, db):
    response = create(client, settings)
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    raw = re.search(r'id="new-mail-key"[^>]+value="([^"]+)"', response.text).group(1)
    token = MailStore(db).authenticate_token(raw)
    assert token['quota_exempt'] == 1
    assert '管理员 · 不限 Hub 发送次数' in response.text
    assert 'Resend 套餐额度和频率限制仍然有效' in response.text
    assert raw not in client.get('/mail').text
    assert raw not in str(dict(db.execute('SELECT * FROM mail_api_tokens').fetchone()))


def test_exempt_key_passes_api_more_than_both_configured_caps(client, settings, admin, db):
    response = create(client, settings, hourly_limit='ignored', daily_limit='ignored')
    assert response.status_code == 200
    raw = re.search(r'id="new-mail-key"[^>]+value="([^"]+)"', response.text).group(1)
    for index in range(3):
        sent = client.post('/api/v1/mail/messages', json={'to': 'different@example.test',
            'subject': 'event', 'text': 'no actual send'}, headers={
                'Authorization': 'Bearer ' + raw, 'Idempotency-Key': f'admin-event-{index}'})
        assert sent.status_code == 202
    assert db.execute('SELECT COUNT(*) FROM mail_deliveries').fetchone()[0] == 3
    assert db.execute('SELECT COUNT(*) FROM email_recipients').fetchone()[0] == 0


def test_default_creation_remains_limited(client, settings, admin, db):
    assert create(client, settings, quota_exempt='').status_code == 200
    row = db.execute('SELECT * FROM mail_api_tokens').fetchone()
    assert row['quota_exempt'] == 0 and row['hourly_limit'] == 1 and row['daily_limit'] == 1


def test_admin_issuance_needs_owner_and_csrf(client, settings, admin, db):
    assert create(client, settings, _csrf='wrong').status_code == 403
    db.execute("UPDATE users SET role='viewer' WHERE id=?", (admin['id'],))
    db.commit()
    assert create(client, settings).status_code == 403
    client.cookies.clear()
    assert create(client, settings).status_code == 401
    assert db.execute('SELECT COUNT(*) FROM mail_api_tokens').fetchone()[0] == 0


def test_sending_key_cannot_promote_itself_or_create_keys(client, settings, admin, db):
    token = MailStore(db).create_token(admin['id'], 'ordinary')
    client.cookies.clear()
    headers = {'Authorization': 'Bearer ' + token['raw'], 'Idempotency-Key': 'ordinary-promotion'}
    assert client.post('/mail/tokens', data={'name': 'elevated', 'quota_exempt': 'on'}, headers=headers).status_code == 401
    result = client.post('/api/v1/mail/messages', headers=headers,
        json={'to': 'someone@example.test', 'subject': 'test', 'text': 'test', 'quota_exempt': True})
    assert result.status_code == 422
    assert db.execute('SELECT quota_exempt FROM mail_api_tokens').fetchone()[0] == 0


def test_exempt_key_can_be_revoked_and_no_longer_authenticates(client, settings, admin, db):
    token = MailStore(db).create_token(admin['id'], 'creator', quota_exempt=True)
    response = client.post('/mail/tokens/' + token['id'] + '/revoke',
        data={'_csrf': client.cookies.get(settings.csrf_cookie_name)}, follow_redirects=False)
    assert response.status_code == 303
    assert client.get('/api/v1/mail/messages', headers={'Authorization': 'Bearer ' + token['raw']}).status_code == 401
