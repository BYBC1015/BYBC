from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie

from codex_hub.security import hash_token


def cookies(response):
    parsed = SimpleCookie()
    for header in response.headers.get_list('set-cookie'):
        parsed.load(header)
    return parsed


def login(client, settings, **kwargs):
    return client.post('/login', data={'username': settings.owner_username,
        'password': settings.owner_password}, follow_redirects=False, **kwargs)


def expire_in(repo, raw, seconds):
    expires = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec='seconds')
    repo.connection.execute('UPDATE sessions SET expires_at=? WHERE session_hash=?', (expires, hash_token(raw)))
    repo.connection.commit()
    return expires


def expiry(repo, raw):
    row = repo.connection.execute('SELECT expires_at FROM sessions WHERE session_hash=?', (hash_token(raw),)).fetchone()
    return row['expires_at'] if row else None


def test_login_persists_both_cookies_for_existing_fourteen_day_session(client, settings, repo):
    response = login(client, settings)
    parsed = cookies(response)
    for name in (settings.session_cookie_name, settings.csrf_cookie_name):
        cookie = parsed[name]
        assert 14 * 86400 - 10 <= int(cookie['max-age']) <= 14 * 86400
        assert cookie['httponly'] and cookie['samesite'] == 'lax' and cookie['path'] == '/'
    raw = client.cookies[settings.session_cookie_name]
    original_expiry = expiry(repo, raw)
    client.get('/projects')
    assert expiry(repo, raw) == original_expiry


def test_authenticated_refresh_migrates_old_session_only_for_remaining_lifetime(client, settings, repo):
    user = repo.get_user_by_username(settings.owner_username)
    session = repo.create_session(user['id'])
    deadline = expire_in(repo, session.raw, 7200)
    client.cookies.set(settings.session_cookie_name, session.raw)
    client.cookies.set(settings.csrf_cookie_name, session.csrf)
    response = client.get('/projects')
    parsed = cookies(response)
    assert parsed[settings.session_cookie_name].value == session.raw
    assert parsed[settings.csrf_cookie_name].value == session.csrf
    for name in (settings.session_cookie_name, settings.csrf_cookie_name):
        assert 7190 <= int(parsed[name]['max-age']) <= 7200
    assert expiry(repo, session.raw) == deadline
    # A later remaining lifetime must be honored, never renewed for fourteen days.
    deadline2 = expire_in(repo, session.raw, 300)
    refreshed = cookies(client.get('/projects'))
    assert 290 <= int(refreshed[settings.session_cookie_name]['max-age']) <= 300
    assert expiry(repo, session.raw) == deadline2


def test_missing_csrf_cookie_is_repaired_and_persisted_without_session_extension(client, settings, repo):
    session = repo.create_session(repo.get_user_by_username(settings.owner_username)['id'])
    deadline = expire_in(repo, session.raw, 3600)
    client.cookies.set(settings.session_cookie_name, session.raw)
    parsed = cookies(client.get('/projects'))
    repaired = parsed[settings.csrf_cookie_name].value
    assert repaired != session.csrf and repo.session_matches_csrf(session.raw, repaired)
    assert 3590 <= int(parsed[settings.csrf_cookie_name]['max-age']) <= 3600
    assert expiry(repo, session.raw) == deadline


def test_expired_session_is_not_migrated_or_refreshed(client, settings, repo):
    login(client, settings)
    raw = client.cookies[settings.session_cookie_name]
    deadline = expire_in(repo, raw, -60)
    response = client.get('/projects', follow_redirects=False)
    assert response.status_code in (302, 303) and response.headers['location'].endswith('/login')
    assert not response.headers.get_list('set-cookie')
    assert not client.get('/login').headers.get_list('set-cookie')
    assert repo.session_cookie_max_age(raw) == 0
    assert expiry(repo, raw) == deadline


def test_logout_deletes_persistent_cookies_and_server_session(client, settings, repo):
    login(client, settings)
    raw = client.cookies[settings.session_cookie_name]
    response = client.post('/logout', data={'_csrf': client.cookies[settings.csrf_cookie_name]}, follow_redirects=False)
    parsed = cookies(response)
    assert all(parsed[name]['max-age'] == '0' for name in (settings.session_cookie_name, settings.csrf_cookie_name))
    assert expiry(repo, raw) is None and repo.session_cookie_max_age(raw) == 0
    assert client.get('/projects', follow_redirects=False).headers['location'].endswith('/login')


def test_persistent_cookies_keep_secure_flag_behind_https_proxy(client, settings):
    parsed = cookies(login(client, settings, headers={'x-forwarded-proto': 'https'}))
    assert parsed[settings.session_cookie_name]['secure']
    assert parsed[settings.csrf_cookie_name]['secure']
