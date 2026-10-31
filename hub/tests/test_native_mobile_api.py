"""Native read/send contract against isolated state; never runs production workers.

Cancellation of a native view request is not cancellation of a Hub task.  The
server assertions below cover revoked sessions, interrupted dispatch cleanup and
receipt-only recovery; native callback generation tests cover late UI responses.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import io
import json
import threading

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from codex_hub.agents import AgentStore
from codex_hub.execution_leases import ExecutionStore
from codex_hub.execution_progress import write_progress
from codex_hub.security import hash_password
from test_execution_leases import lease_task, start  # noqa: F401
from workspace_fixture import seed_workspace


BASE = '/api/mobile'
PROJECT = BASE + '/projects/workspace-team'
WORK_TABLES = (
    'tasks', 'entries', 'attachments', 'hub_task_attachments',
    'hub_conversations', 'hub_conversation_agents', 'hub_agent_dispatches',
    'hub_dispatch_tasks', 'hub_task_executions', 'hub_execution_progress',
)


def login(client, username='owner', password='owner-password'):
    response = client.post(BASE + '/login', json={'username': username, 'password': password})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['api_version'] == 1
    assert data['user']['username'] == username
    assert data['csrf_token'] and data['session_key']
    assert 'password' not in data['user'] and 'password_hash' not in data['user']
    return data


@pytest.fixture
def native_team(client, repo):
    data = seed_workspace(repo, room_messages=75)
    data['session'] = login(client)
    return data


def body(data, **changes):
    return {'description': '原生消息，保留已有项目要求',
            'conversation_id': data['conversation_id'],
            'agent_ids': [data['engineer']['id']],
            'request_key': 'native-work-request-0001', **changes}


def send(client, data, **changes):
    return client.post(PROJECT + '/messages', json=body(data, **changes),
                       headers={'X-CSRF-Token': data['session']['csrf_token']})


def upload(client, data, *, files=None, **changes):
    fields = body(data, **changes)
    fields['_csrf'] = data['session']['csrf_token']
    fields['agent_ids'] = [str(value) for value in fields['agent_ids']]
    return client.post(PROJECT + '/messages', data=fields, files=files or [
        ('attachments', ('brief.txt', b'native isolated brief', 'text/plain'))])


def state(repo, settings):
    tables = {row[0] for row in repo.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {
        'rows': {name: [tuple(row) for row in repo.connection.execute(f'SELECT * FROM {name} ORDER BY rowid')]
                 for name in WORK_TABLES if name in tables},
        'files': {str(path.relative_to(settings.files_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in settings.files_dir.rglob('*') if path.is_file()},
    }


def add_other_project(repo, data):
    project = repo.create_project(slug='native-other-project', name='另一隔离项目',
                                  created_by_device_id=data['connection']['id'])
    agent = data['store'].create(project['id'], '其他项目智能体', '只做其他项目的工作',
                                 device_id=data['connection']['id'])
    task = repo.create_task(project['id'], '其他项目任务', assigned_agent_id=agent['id'])
    room = data['store'].route(task['id'], {'action': 'new', 'title': '其他会话', 'reason': '隔离测试'}, 'codex_cli')
    return project, agent, task, room['conversation_id']


@pytest.mark.parametrize('path', [
    '/session', '/projects', '/toolbox', '/projects/workspace-team/workspace',
    '/projects/workspace-team/devices', '/projects/workspace-team/library',
    '/projects/workspace-team/tasks/1', '/projects/workspace-team/receipts/no-send-00001',
])
def test_native_reads_require_session_and_never_redirect_to_html(client, path):
    response = client.get(BASE + path, headers={'Accept': 'text/html'}, follow_redirects=False)
    assert response.status_code == 401
    assert response.headers['content-type'].startswith('application/json')
    assert response.headers['cache-control'] == 'no-store'
    assert 'location' not in response.headers
    assert response.json()['detail']


def test_device_token_is_not_a_native_owner_session(client, repo):
    device = repo.create_device(label='Synthetic token is not a login')
    token = repo.create_api_token(device_id=device['id'], name='native-auth-test')
    response = client.get(BASE + '/projects', headers={'Authorization': f'Bearer {token.raw}'})
    assert response.status_code == 401


def test_login_session_csrf_and_logout_revoke_captured_cookie(client, repo, settings):
    assert client.post(BASE + '/login', json={'username': 'owner', 'password': 'incorrect'}).status_code == 401
    first = login(client)
    old_cookies = dict(client.cookies)
    assert client.get(BASE + '/session').json()['session_key'] == first['session_key']
    assert first['session_key'] not in old_cookies.values()
    assert client.post(BASE + '/logout', headers={'X-CSRF-Token': 'wrong'}).status_code == 403
    assert client.get(BASE + '/session').status_code == 200
    response = client.post(BASE + '/logout', headers={'X-CSRF-Token': first['csrf_token']})
    assert response.status_code == 200
    assert client.get(BASE + '/session').status_code == 401
    stale = TestClient(client.app)
    stale.cookies.update(old_cookies)
    assert stale.get(BASE + '/session').status_code == 401
    second = login(client)
    assert second['session_key'] != first['session_key']
    assert second['csrf_token'] != first['csrf_token']
    assert settings.session_cookie_name in client.cookies


@pytest.mark.parametrize('old_csrf', ['', 'invalid-restored-csrf'])
def test_native_session_repairs_partial_cookie_migration_without_extending_login(client, repo, settings, old_csrf):
    first = login(client)
    raw = client.cookies[settings.session_cookie_name]
    expiry = repo.connection.execute('SELECT expires_at FROM sessions').fetchone()[0]
    client.cookies.clear()
    client.cookies.set(settings.session_cookie_name, raw)
    if old_csrf:
        client.cookies.set(settings.csrf_cookie_name, old_csrf)
    response = client.get(BASE + '/session')
    assert response.status_code == 200
    recovered = response.json()
    assert recovered['session_key'] == first['session_key']
    assert recovered['csrf_token'] and recovered['csrf_token'] != old_csrf
    assert repo.session_matches_csrf(raw, recovered['csrf_token'])
    assert settings.csrf_cookie_name + '=' in response.headers.get('set-cookie', '')
    assert repo.connection.execute('SELECT expires_at FROM sessions').fetchone()[0] == expiry


@pytest.mark.parametrize('payload', [[], {'username': 'owner'}, {'username': ['owner'], 'password': 'x'}])
def test_malformed_login_is_json_error_not_500(client, payload):
    response = client.post(BASE + '/login', json=payload)
    assert response.status_code in {400, 401, 422}
    assert response.headers['content-type'].startswith('application/json')


@pytest.mark.parametrize('csrf', [None, '', 'unrelated-csrf'])
def test_mutating_messages_need_session_bound_csrf(client, repo, settings, native_team, csrf):
    before = state(repo, settings)
    headers = {} if csrf is None else {'X-CSRF-Token': csrf}
    response = client.post(PROJECT + '/messages', json=body(native_team), headers=headers)
    assert response.status_code == 403
    assert state(repo, settings) == before


def test_old_session_csrf_cannot_authorize_new_session_or_revoked_send(client, repo, settings, native_team):
    data = native_team
    first = data['session']
    old = TestClient(client.app)
    old.cookies.update(dict(client.cookies))
    assert client.post(BASE + '/logout', headers={'X-CSRF-Token': first['csrf_token']}).status_code == 200
    login(client)
    before = state(repo, settings)
    assert client.post(PROJECT + '/messages', json=body(data),
                       headers={'X-CSRF-Token': first['csrf_token']}).status_code == 403
    assert old.post(PROJECT + '/messages', json=body(data),
                    headers={'X-CSRF-Token': first['csrf_token']}).status_code == 401
    assert state(repo, settings) == before


def test_workspace_returns_native_models_latest_new_and_bounded_history(client, repo, settings, native_team):
    data = native_team
    before = state(repo, settings)
    latest = client.get(PROJECT + '/workspace').json()
    assert latest['conversation']['id'] == data['conversation_id']
    assert latest['entries'] == latest['messages']
    assert 0 < len(latest['messages']) <= 40
    assert latest['session_key'] == data['session']['session_key']
    assert latest['csrf_token'] == data['session']['csrf_token']
    assert latest['default_agent_id'] == data['controller']['id']
    assert all(type(agent['active']) is bool and type(agent['can_dispatch']) is bool for agent in latest['agents'])
    assert all(type(task['in_history']) is bool and type(task['archive_eligible']) is bool for task in latest['tasks'])
    assert all(type(device['device_id']) is int for device in latest['devices'])
    assert latest['older_entries_before']
    assert latest['revision']
    assert all({'id', 'author', 'role', 'text', 'task_id', 'attachments'} <= item.keys() for item in latest['messages'])
    assert not any(key in latest for key in ('html', 'body_html', 'workspace_html'))
    older = client.get(PROJECT + '/workspace', params={
        'conversation_id': data['conversation_id'], 'before': latest['older_entries_before']}).json()
    assert not ({row['id'] for row in latest['messages']} & {row['id'] for row in older['messages']})
    assert older['session_key'] == latest['session_key']
    assert client.get(PROJECT + '/workspace', params={
        'conversation_id': data['conversation_id'], 'before_entry_id': latest['older_entries_before']}).json()['messages'] == older['messages']
    blank = client.get(PROJECT + '/workspace?conversation_id=0').json()
    assert blank['conversation'] is None and blank['messages'] == [] and blank['progress'] == []
    assert state(repo, settings) == before


def test_native_read_routes_are_read_only_and_toolbox_has_local_actions(client, repo, settings, native_team):
    data = native_team
    before = state(repo, settings)
    paths = [BASE + '/projects', BASE + '/toolbox', PROJECT + '/workspace', PROJECT + '/devices',
             PROJECT + '/library', PROJECT + '/tasks?history=1', PROJECT + f"/tasks/{data['tasks'][2]['id']}",
             PROJECT + '/receipts/never-submitted-0001']
    for _ in range(2):
        for path in paths:
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert response.headers['content-type'].startswith('application/json')
            assert response.headers['cache-control'] == 'no-store'
    tools = client.get(BASE + '/toolbox').json()['tools']
    assert {'library', 'devices', 'feedback', 'downloads'} <= {tool['action'] for tool in tools}
    assert state(repo, settings) == before


def test_foreign_project_conversation_task_and_agent_cannot_be_mixed(client, repo, settings, native_team):
    data = native_team
    _project, agent, task, room = add_other_project(repo, data)
    before = state(repo, settings)
    assert client.get(PROJECT + f'/workspace?conversation_id={room}').status_code == 404
    assert client.get(PROJECT + f"/tasks/{task['id']}").status_code == 404
    assert send(client, data, conversation_id=room).status_code in {404, 422}
    assert send(client, data, agent_ids=[agent['id']]).status_code in {404, 422}
    assert state(repo, settings) == before


@pytest.mark.parametrize('parameter', ['before', 'before_entry_id'])
@pytest.mark.parametrize('origin', ['other_project', 'other_room', 'missing'])
def test_pagination_cursor_cannot_silently_become_latest_or_cross_room(client, repo, settings, native_team, parameter, origin):
    data = native_team
    if origin == 'missing':
        entry_id = 999999999
    else:
        if origin == 'other_project':
            project, _, task, _ = add_other_project(repo, data)
        else:
            project = data['project']
            task = repo.create_task(project['id'], '另一会话的翻页记录', assigned_agent_id=data['engineer']['id'])
            data['store'].route(task['id'], {'action': 'new', 'title': '另一会话', 'reason': '隔离游标'}, 'codex_cli')
        entry_id = repo.create_entry(project_id=project['id'], task_id=task['id'],
            source_label='isolated cursor', kind='message', body_markdown='不属于当前对话的记录')['id']
    before = state(repo, settings)
    response = client.get(PROJECT + '/workspace', params={
        'conversation_id': data['conversation_id'], parameter: entry_id})
    assert response.status_code in {404, 422}, response.text
    assert state(repo, settings) == before


def test_atomic_group_upload_has_one_user_message_and_one_copy_per_file(client, repo, settings, native_team):
    data = native_team
    result = upload(client, data, agent_ids=[data['engineer']['id'], data['reviewer']['id']], files=[
        ('attachments', ('brief.txt', b'one brief', 'text/plain')),
        ('attachments', ('../../picture.png', b'picture fixture', 'image/png'))])
    assert result.status_code in {200, 201}, result.text
    saved = result.json()
    assert saved['created'] is True and len(saved['task_ids']) == 2
    assert saved['conversation_id'] == data['conversation_id']
    manifests = [repo.list_task_attachments(data['project']['id'], task_id) for task_id in saved['task_ids']]
    assert [item['id'] for item in manifests[0]] == [item['id'] for item in manifests[1]]
    assert {item['original_filename'] for item in manifests[0]} == {'brief.txt', 'picture.png'}
    assert len({item['entry_id'] for item in manifests[0]}) == 1
    entry = repo.get_entry(manifests[0][0]['entry_id'])
    assert entry['author_type'] == 'user' and entry['body_markdown'] == body(data)['description']
    assert set(json.loads(entry['metadata_json'])['task_ids']) == set(saved['task_ids'])
    assert len(state(repo, settings)['files']) == 2
    visible = client.get(PROJECT + '/workspace').json()['messages']
    sent_messages = [row for row in visible if row['id'] == entry['id']]
    assert len(sent_messages) == 1 and sent_messages[0]['role'] == 'user'
    assert {item['name'] for item in sent_messages[0]['attachments']} == {'brief.txt', 'picture.png'}


def test_exact_attachment_retry_is_stable_and_does_not_store_again(client, repo, settings, native_team, monkeypatch):
    import codex_hub.main as main
    data = native_team
    files = [('attachments', ('one.txt', b'one', 'text/plain')), ('attachments', ('two.txt', b'two', 'text/plain'))]
    first = upload(client, data, files=files)
    assert first.status_code in {200, 201}, first.text
    before = state(repo, settings)
    async def forbid_store(*args, **kwargs):
        raise AssertionError('idempotent retry must not store any new file')
    monkeypatch.setattr(main, 'store_attachment_file', forbid_store)
    retried = upload(client, data, files=list(reversed(files)))
    assert retried.status_code in {200, 201}, retried.text
    assert retried.json()['created'] is False
    assert retried.json()['task_ids'] == first.json()['task_ids']
    assert state(repo, settings) == before


@pytest.mark.parametrize('changes', [
    {'description': 'different content'}, {'title': 'different title'}, {'conversation_id': 0},
])
def test_reusing_send_key_for_different_payload_is_409(client, repo, settings, native_team, changes):
    assert send(client, native_team).status_code in {200, 201}
    before = state(repo, settings)
    assert send(client, native_team, **changes).status_code == 409
    assert state(repo, settings) == before


@pytest.mark.parametrize('filename,content,mime', [
    ('other.txt', b'native isolated brief', 'text/plain'),
    ('brief.txt', b'changed bytes', 'text/plain'),
    ('brief.txt', b'native isolated brief', 'application/octet-stream'),
])
def test_changed_attachment_under_same_key_is_409(client, repo, settings, native_team, filename, content, mime):
    assert upload(client, native_team).status_code in {200, 201}
    before = state(repo, settings)
    assert upload(client, native_team, files=[('attachments', (filename, content, mime))]).status_code == 409
    assert state(repo, settings) == before


def test_lost_response_recovers_by_receipt_without_replay(client, repo, settings, native_team):
    data = native_team
    key = body(data)['request_key']
    before = state(repo, settings)
    for _ in range(3):
        assert client.get(PROJECT + '/receipts/' + key).json()['found'] is False
    assert state(repo, settings) == before
    sent = upload(client, data)  # Simulates transport committed, native response was discarded.
    assert sent.status_code in {200, 201}, sent.text
    before = state(repo, settings)
    for _ in range(3):
        receipt = client.get(PROJECT + '/receipts/' + key).json()
        assert receipt['found'] is True
        assert receipt['task_ids'] == sent.json()['task_ids']
        assert receipt['conversation_id'] == sent.json()['conversation_id']
        client.get(PROJECT + '/workspace')
    assert state(repo, settings) == before


def test_receipts_and_key_namespace_are_user_and_project_scoped(client, repo, settings, native_team):
    data = native_team
    first = send(client, data)
    assert first.status_code in {200, 201}
    add_other_project(repo, data)
    other_id = repo.connection.execute("INSERT INTO users(username,password_hash,role,created_at,updated_at) VALUES (?,?, 'owner',?,?)",
        ('native-other-owner', hash_password('native-other-pass'), '2026-10-31', '2026-10-31')).lastrowid
    repo.connection.commit()
    key = body(data)['request_key']
    before = state(repo, settings)
    assert client.get(BASE + '/projects/native-other-project/receipts/' + key).json()['found'] is False
    another = TestClient(client.app)
    other_session = login(another, 'native-other-owner', 'native-other-pass')
    assert other_session['session_key'] != data['session']['session_key']
    assert another.get(PROJECT + '/receipts/' + key).json()['found'] is False
    assert state(repo, settings) == before
    second = send(another, {**data, 'session': other_session})
    assert second.status_code in {200, 201}
    assert second.json()['created'] is True
    assert set(second.json()['task_ids']).isdisjoint(first.json()['task_ids'])
    assert all(repo.get_task(tid)['created_by_user_id'] == other_id for tid in second.json()['task_ids'])
    assert client.get(PROJECT + '/receipts/' + key).json()['task_ids'] == first.json()['task_ids']


@pytest.mark.parametrize('changes', [
    {'description': '', 'title': ''}, {'description': 'x' * 16001}, {'title': 'x' * 201},
    {'agent_ids': []}, {'agent_ids': [True]}, {'agent_ids': ['not-an-id']},
    {'conversation_id': -1}, {'conversation_id': 'oops'}, {'request_key': 'short'},
])
def test_invalid_send_never_creates_work(client, repo, settings, native_team, changes):
    before = state(repo, settings)
    response = send(client, native_team, **changes)
    assert response.status_code in {400, 413, 422}, response.text
    assert state(repo, settings) == before


def test_upload_limits_and_inactive_recipient_do_not_leave_files(client, repo, settings, native_team):
    data = native_team
    before = state(repo, settings)
    assert upload(client, data, agent_ids=[data['paused']['id']]).status_code in {404, 422}
    files = [('attachments', (f'file-{index}.txt', b'one', 'text/plain')) for index in range(13)]
    assert upload(client, data, files=files).status_code in {400, 413, 422}
    assert state(repo, settings) == before


def test_file_only_send_and_private_native_download(client, repo, settings, native_team):
    data = native_team
    image = io.BytesIO()
    Image.new('RGB', (12, 8), 'navy').save(image, format='PNG')
    raw = image.getvalue()
    response = upload(client, data, description='', conversation_id=0,
                      files=[('attachments', ('native.png', raw, 'image/png'))])
    assert response.status_code in {200, 201}, response.text
    saved = response.json()
    assert saved['conversation_id'] != data['conversation_id']
    messages = client.get(PROJECT + f"/workspace?conversation_id={saved['conversation_id']}").json()['messages']
    attachment = next(item for row in messages for item in row['attachments'])
    assert attachment['name'] == 'native.png' and attachment['mime_type'] == 'image/png'
    download = client.get(attachment['download_url'])
    assert download.status_code == 200 and download.content == raw
    assert download.headers['content-disposition'].startswith('attachment;')
    assert download.headers['x-content-type-options'] == 'nosniff'
    preview = client.get(attachment['preview_url'])
    assert preview.status_code == 200 and preview.headers['content-type'].startswith('image/')
    with Image.open(io.BytesIO(preview.content)) as decoded:
        assert decoded.size == (12, 8)
    assert client.get(PROJECT + '/library').json()['attachments']
    unsigned = TestClient(client.app)
    denied = unsigned.get(attachment['download_url'], follow_redirects=False)
    assert denied.status_code in {401, 303} and denied.content != raw
    # Existing private web-file URLs may redirect to login. Native transport
    # rejects the redirect; it must not treat that HTML as an image/download.
    if denied.status_code == 303:
        assert '/login' in denied.headers['location']


def test_interrupted_dispatch_cleans_new_files_without_work_commit(client, repo, settings, native_team, monkeypatch):
    before = state(repo, settings)
    def interrupted(*args, **kwargs):
        raise asyncio.CancelledError('synthetic disconnected request before dispatch commit')
    monkeypatch.setattr(AgentStore, 'dispatch', interrupted)
    try:
        upload(client, native_team)
    except (asyncio.CancelledError, concurrent.futures.CancelledError):
        pass
    except RuntimeError as exc:
        # Starlette's middleware turns a cancelled ASGI response into this
        # transport error; it must still execute the upload cleanup finally.
        assert str(exc) == 'No response returned.'
    else:
        pytest.fail('the synthetic cancelled request unexpectedly completed')
    assert state(repo, settings) == before


def test_logout_during_upload_validation_fences_old_inflight_send(client, repo, settings, native_team, monkeypatch):
    import codex_hub.mobile_routes as routes
    original = routes.inspect_workspace_uploads
    raw_session = client.cookies[settings.session_cookie_name]
    async def logout_after_inspection(request, uploads):
        manifest = await original(request, uploads)
        repo.delete_session(raw_session)
        return manifest
    monkeypatch.setattr(routes, 'inspect_workspace_uploads', logout_after_inspection)
    before = state(repo, settings)
    result = upload(client, native_team)
    assert result.status_code == 401, result.text
    assert state(repo, settings) == before


def test_concurrent_explicit_retry_commits_one_dispatch(client, repo, settings, native_team):
    data = native_team
    cookies = dict(client.cookies)
    initial = len(repo.list_tasks(data['project']['id']))
    def post_one(_):
        isolated_client = TestClient(client.app)
        isolated_client.cookies.update(cookies)
        result = upload(isolated_client, data)
        assert result.status_code in {200, 201}, result.text
        return result.json()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(post_one, range(2)))
    assert results[0]['task_ids'] == results[1]['task_ids']
    assert sorted(result['created'] for result in results) == [False, True]
    assert len(repo.list_tasks(data['project']['id'])) == initial + 1
    assert len(state(repo, settings)['files']) == 1


def test_concurrent_conflicting_same_key_has_one_winner_and_no_orphan_files(client, repo, settings, native_team, monkeypatch):
    data = native_team
    cookies = dict(client.cookies)
    initial = len(repo.list_tasks(data['project']['id']))
    original = AgentStore.dispatch
    ready = threading.Barrier(2)
    def simultaneous_commit(self, *args, **kwargs):
        ready.wait(timeout=10)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(AgentStore, 'dispatch', simultaneous_commit)
    def post_one(index):
        isolated_client = TestClient(client.app)
        isolated_client.cookies.update(cookies)
        return upload(isolated_client, data, description=f'competing explicit content {index}',
                      files=[('attachments', (f'variant-{index}.txt', f'bytes-{index}'.encode(), 'text/plain'))])
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(post_one, range(2)))
    assert sorted(result.status_code for result in results) == [200, 409]
    winner = next(result.json() for result in results if result.status_code == 200)
    assert winner['created'] is True and len(winner['task_ids']) == 1
    assert len(repo.list_tasks(data['project']['id'])) == initial + 1
    assert len(state(repo, settings)['files']) == 1


def test_progress_polling_cannot_take_over_or_restart_stale_execution(client, repo, settings, lease_task):
    data = lease_task
    login(client)
    store = ExecutionStore(repo)
    run = start(store, data)
    write_progress(store, data['task']['id'], data['device']['id'], {
        'generation': run['generation'], 'run_key': run['run_key'], 'sequence': 1,
        'kind': 'assistant', 'text': '真实进展：测试已完成第一步'})
    path = BASE + '/projects/lease-testing'
    response = client.get(path + '/workspace').json()
    assert any(item['assistant_text'] == '真实进展：测试已完成第一步' for item in response['progress'])
    assert all('run_key' not in item for item in response['progress'])
    repo.connection.execute('UPDATE hub_task_executions SET lease_until=0 WHERE task_id=?', (data['task']['id'],))
    repo.connection.commit()
    before = state(repo, settings)
    for _ in range(3):
        assert client.get(path + '/workspace').status_code == 200
        assert client.get(path + f"/tasks/{data['task']['id']}").status_code == 200
    assert state(repo, settings) == before


def test_feedback_is_retry_safe_and_does_not_dispatch_production_work(client, repo, settings, native_team):
    data = native_team
    headers = {'X-CSRF-Token': data['session']['csrf_token']}
    report = {'request_key': 'native-feedback-request-0001', 'kind': 'bug', 'title': '移动端测试反馈',
              'body': '切换网络后原生页面应仍可访问', 'conversation_id': data['conversation_id'],
              'task_id': data['tasks'][0]['id'], 'platform': 'android-native-test'}
    before = state(repo, settings)
    assert client.post(PROJECT + '/feedback', json=report).status_code == 403
    first = client.post(PROJECT + '/feedback', json=report, headers=headers)
    assert first.status_code in {200, 201}, first.text
    second = client.post(PROJECT + '/feedback', json=report, headers=headers)
    assert second.json()['feedback']['id'] == first.json()['feedback']['id']
    assert state(repo, settings) == before

