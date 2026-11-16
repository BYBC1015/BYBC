"""Modern settings have real, permission-checked JSON mutations."""
import pytest

from codex_hub.model_policy import agent_policy, ensure_task_choice, task_choice
from codex_hub.settings_routes import register_settings_routes
from test_model_policy import setup_team


@pytest.fixture(autouse=True)
def register_settings_if_needed(app):
    if not any(getattr(route, 'path', '') == '/api/projects/{slug}/settings' for route in app.routes):
        register_settings_routes(app)


def login(client):
    response = client.post('/login', data={'username':'owner', 'password':'owner-password'}, follow_redirects=False)
    assert response.status_code == 303
    return {'x-csrf-token':client.cookies['codex_hub_csrf']}


def test_settings_read_and_actual_member_primary_mutations(client, repo, settings):
    project, device, _, _, _, _ = setup_team(repo, settings)
    headers = login(client)
    base = '/api/projects/model-team/settings'
    before_tasks = repo.connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
    initial = client.get(base).json()
    assert initial['can_manage'] and len(initial['agents']) == 2
    spare = repo.create_device('Spare execution computer')
    assert any(item['id'] == spare['id'] for item in client.get(base).json()['available_devices'])
    added = client.post(base + '/members', json={'device_id':spare['id'], 'permission':'write'}, headers=headers)
    assert added.status_code == 200 and added.headers['content-type'].startswith('application/json')
    assert any(item['device_id'] == spare['id'] for item in added.json()['members'])
    primary = client.post(base + '/primary-device', json={'device_id':spare['id']}, headers=headers)
    assert primary.status_code == 200 and primary.json()['primary_device_id'] == spare['id']
    removed = client.delete(base + '/members/' + str(spare['id']), headers=headers)
    assert removed.status_code == 200 and removed.json()['primary_device_id'] is None
    assert all(item['device_id'] != spare['id'] for item in removed.json()['members'])
    assert repo.connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == before_tasks


@pytest.mark.parametrize('payload', [
    {'device_id':True, 'permission':'write'}, {'device_id':'../../', 'permission':'write'},
    {'device_id':None, 'permission':'write'}, {'device_id':1,'permission':[]},
])
def test_member_invalid_payload_is_rejected(client, repo, settings, payload):
    setup_team(repo, settings)
    headers = login(client)
    response = client.post('/api/projects/model-team/settings/members', json=payload, headers=headers)
    assert response.status_code == 422


def test_settings_require_session_owner_and_csrf(client, repo, settings):
    _, device, _, _, _, _ = setup_team(repo, settings)
    base = '/api/projects/model-team/settings'
    assert client.get(base).status_code == 401
    headers = login(client)
    assert client.post(base + '/members', json={'device_id':device['id'],'permission':'read'}).status_code == 403
    assert client.post(base + '/members', json={'device_id':device['id'],'permission':'read','_csrf':headers['x-csrf-token']}).status_code == 200
    repo.connection.execute("UPDATE users SET role='viewer' WHERE username='owner'")
    repo.connection.commit()
    assert not client.get(base).json()['can_manage']
    assert client.post(base + '/members', json={'device_id':device['id'],'permission':'write'}, headers=headers).status_code == 403
    assert client.delete(base + '/members/' + str(device['id']), headers=headers).status_code == 403


def test_setting_primary_checks_project_membership_and_write_permission(client, repo, settings):
    setup_team(repo, settings)
    headers = login(client)
    other = repo.create_device('Foreign device')
    base = '/api/projects/model-team/settings'
    assert client.post(base + '/primary-device', json={'device_id':other['id']}, headers=headers).status_code == 422
    client.post(base + '/members', json={'device_id':other['id'],'permission':'read'}, headers=headers)
    assert client.post(base + '/primary-device', json={'device_id':other['id']}, headers=headers).status_code == 422


def test_settings_policy_is_reversible_and_frozen_task_unchanged(client, repo, settings):
    project, device, _, _, worker, _ = setup_team(repo, settings)
    headers = login(client)
    task = repo.create_task(project['id'], 'Existing model choice', assigned_agent_id=worker['id'])
    frozen = ensure_task_choice(repo, task, settings.data_dir)
    base = f'/api/projects/model-team/settings/agents/{worker["id"]}/model-policy'
    loaded = client.get(base)
    assert loaded.status_code == 200 and loaded.json()['policy']['mode'] == 'controller'
    assert len(loaded.json()['models']) == 3
    saved = client.post(base, json={'mode':'manual','model':'gpt-5.6-sol','reasoning_effort':'high'}, headers=headers)
    assert saved.status_code == 200
    assert agent_policy(repo.connection, worker['id'])['model'] == 'gpt-5.6-sol'
    assert task_choice(repo.connection, task['id']) == frozen
    assert client.post(base, json={'mode':'manual','model':'imaginary-model','reasoning_effort':'high'}, headers=headers).status_code == 422
    assert client.post(base, json={'mode':'controller'}, headers=headers).status_code == 200
    assert agent_policy(repo.connection, worker['id'])['model'] == ''


def test_settings_policy_does_not_read_foreign_agent(client, repo, settings):
    project, device, store, _, worker, _ = setup_team(repo, settings)
    other = repo.create_project('other-settings', 'Other', created_by_device_id=device['id'])
    foreign = store.create(other['id'], 'Other worker', 'Other', device_id=device['id'])
    headers = login(client)
    base = f'/api/projects/model-team/settings/agents/{foreign["id"]}/model-policy'
    assert client.get(base).status_code == 404
    assert client.post(base, json={'mode':'controller'}, headers=headers).status_code == 404
