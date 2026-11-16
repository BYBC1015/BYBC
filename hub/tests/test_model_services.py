"""Real project/lease contracts; model requests are isolated local fixtures."""
import json

import pytest

from codex_hub.device_capabilities import save_device_capabilities
from codex_hub.execution_leases import ExecutionStore
from codex_hub.migration import _iter_plain_files
from codex_hub.model_policy import available_models, ensure_task_choice, save_policy, task_choice
from codex_hub.model_services import ModelServices
from test_agents import token_headers
from test_execution_leases import lease_task, start
from test_native_mobile_api import login


@pytest.fixture
def services(client, repo, settings, lease_task):
    session = login(client)
    data = lease_task
    data.update(headers={'X-CSRF-Token': session['csrf_token']},
                path='/api/projects/lease-testing/model-services',
                store=ModelServices(repo.connection, settings.data_dir))
    save_device_capabilities(settings.data_dir, data['device']['id'], {'model_services': True})
    return data


def config(data, **overrides):
    return {'name': '本机模型', 'kind': 'ollama', 'base_url': 'http://127.0.0.1:11434/v1',
            'device_id': data['device']['id'], 'model': 'local-chat', 'supports_tools': True, **overrides}


def create(client, data, **overrides):
    response = client.post(data['path'], headers=data['headers'], json=config(data, **overrides))
    assert response.status_code == 200, response.text
    return response.json()['id']


def test_owner_crud_never_returns_secret_and_preserves_blank_edit(client, repo, settings, services):
    data = services
    sid = create(client, data, api_key='test-provider-secret-value')
    response = client.get(data['path'])
    assert 'test-provider-secret-value' not in response.text
    assert response.json()['services'][0]['has_api_key'] is True
    assert not any('test-provider-secret-value' in str(tuple(row)) for row in repo.connection.execute('SELECT * FROM hub_model_service_versions'))
    assert client.put(f'{data["path"]}/{sid}', headers=data['headers'], json={'name': '新名称', 'api_key': ''}).status_code == 200
    assert data['store'].version(data['project']['id'], sid, 2, data['device']['id'])['api_key'] == 'test-provider-secret-value'
    assert client.put(f'{data["path"]}/{sid}', headers=data['headers'], json={'clear_api_key': True}).status_code == 200
    assert data['store'].version(data['project']['id'], sid, 3, data['device']['id'])['api_key'] == ''
    assert not any(str(rel).startswith('.private') for _, rel in _iter_plain_files(settings.data_dir))
    assert client.delete(f'{data["path"]}/{sid}', headers=data['headers']).status_code == 200
    assert client.get(data['path']).json()['services'] == []


@pytest.mark.parametrize('override', [
    {'base_url': 'http://user:secret@example.com/v1'}, {'base_url': 'https://example.com/v1?key=secret'},
    {'base_url': 'ftp://example.com'}, {'device_id': True}, {'kind': 'unsupported'},
    {'api_key': 'test\nheader'}, {'supports_tools': 'false'}, {'model': ''},
])
def test_rejects_ambiguous_credentials_endpoints_and_types(client, services, override):
    assert client.post(services['path'], headers=services['headers'], json=config(services, **override)).status_code == 422


def test_no_cookie_auth_csrf_or_cross_project_write(client, repo, services):
    data = services
    assert client.post(data['path'], json=config(data)).status_code == 403
    sid = create(client, data)
    project = repo.create_project(slug='unrelated-models', name='隔离项目')
    other = '/api/projects/unrelated-models/model-services'
    assert client.put(other + f'/{sid}', headers=data['headers'], json={'name':'偷换'}).status_code == 422
    assert client.delete(other + f'/{sid}', headers=data['headers']).status_code == 404
    client.cookies.clear()
    response = client.get(data['path'], headers={'Accept': 'text/html'}, follow_redirects=False)
    assert response.status_code == 401 and response.headers['content-type'].startswith('application/json')


def test_host_change_needs_fresh_key_and_restored_missing_key_fails_closed(client, services):
    data = services
    sid = create(client, data, api_key='test-key-value')
    response = client.put(f'{data["path"]}/{sid}', headers=data['headers'], json={'base_url':'https://different.example/v1'})
    assert response.status_code == 422
    for path in data['store'].private_dir.iterdir():
        path.unlink()
    with pytest.raises(ValueError, match='凭据不可用'):
        data['store'].version(data['project']['id'], sid, 1, data['device']['id'])


def test_probe_queue_is_idempotent_device_scoped_and_revision_fenced(client, repo, services):
    data = services
    sid = create(client, data)
    path = f'{data["path"]}/{sid}/test'
    a = client.post(path, headers=data['headers'], json={}).json()
    b = client.post(path, headers=data['headers'], json={}).json()
    assert a['job_id'] == b['job_id']
    other = repo.create_device(label='其他设备')
    assert data['store'].claim_probe(other['id']) is None
    job = data['store'].claim_probe(data['device']['id'])
    assert job['id'] == a['job_id']
    assert data['store'].claim_probe(data['device']['id']) is None
    with pytest.raises(ValueError):
        data['store'].complete_probe(other['id'], job['id'], {'code':'ready','claim_token':job['claim_token']})
    client.put(f'{data["path"]}/{sid}', headers=data['headers'], json={'model':'local-chat-new'})
    data['store'].complete_probe(data['device']['id'], job['id'], {'code':'ready','models':['old'], 'claim_token':job['claim_token']})
    row = client.get(data['path']).json()['services'][0]
    assert row['revision'] == 2 and row['status'] == 'unverified' and row['models'] == []


def test_probe_reports_only_known_codes_and_bounded_model_names(client, services):
    data = services
    sid = create(client, data)
    data['store'].queue_probe(data['project']['id'], sid)
    job = data['store'].claim_probe(data['device']['id'])
    data['store'].complete_probe(data['device']['id'], job['id'],
        {'code':'ready','models':['local-chat','local-chat','<script>'], 'claim_token':job['claim_token'], 'raw_body':'secret'})
    row = client.get(data['path']).json()['services'][0]
    assert row['status'] == 'ready' and row['models'] == ['local-chat']
    assert 'secret' not in json.dumps(row)


def test_frozen_service_revision_only_exact_running_device_can_fetch(client, repo, settings, services):
    data = services
    sid = create(client, data, api_key='old-test-key')
    model = f'hub-service/{sid}'
    save_policy(repo.connection, data['worker'], settings.data_dir, mode='manual', model=model, reasoning_effort='none')
    repo.connection.execute("UPDATE tasks SET execution_route='direct' WHERE id=?", (data['task']['id'],))
    repo.connection.commit()
    task = repo.get_task(data['task']['id'])
    chosen = ensure_task_choice(repo, task, settings.data_dir)
    assert chosen['service_revision'] == 1
    run = start(ExecutionStore(repo), data)
    client.put(f'{data["path"]}/{sid}', headers=data['headers'], json={'api_key':'new-test-key'})
    client.delete(f'{data["path"]}/{sid}', headers=data['headers'])
    path = f'/api/projects/lease-testing/tasks/{task["id"]}/model-service'
    headers = token_headers(repo, data['device'])
    response = client.post(path, headers=headers, json={'execution':run})
    assert response.status_code == 200 and response.json()['service']['api_key'] == 'old-test-key'
    assert response.headers['cache-control'] == 'no-store'
    bad = client.post(path, headers=headers, json={'execution':{**run,'run_key':'wrong'}})
    assert bad.status_code == 409
    repo.update_task_status(task['id'], 'cancelled')
    assert client.post(path, headers=headers, json={'execution':run}).status_code == 409
    assert task_choice(repo.connection, task['id'])['service_revision'] == 1


def test_catalog_excludes_other_projects_and_unsupported_workers(client, repo, settings, services):
    data = services
    create(client, data)
    assert available_models(settings.data_dir, data['device']['id'], repo.connection, data['project']['id'])
    assert available_models(settings.data_dir, data['device']['id'], repo.connection, 9999) == []
    save_device_capabilities(settings.data_dir, data['device']['id'], {})
    assert available_models(settings.data_dir, data['device']['id'], repo.connection, data['project']['id']) == []


def test_api_model_does_not_silently_take_local_engineering(client, repo, settings, services):
    data = services
    sid = create(client, data)
    save_policy(repo.connection, data['worker'], settings.data_dir, mode='manual', model=f'hub-service/{sid}', reasoning_effort='none')
    repo.connection.execute("UPDATE tasks SET execution_route='project' WHERE id=?", (data['task']['id'],))
    repo.connection.commit()
    with pytest.raises(ValueError, match='本地工程'):
        ensure_task_choice(repo, repo.get_task(data['task']['id']), settings.data_dir)


def test_native_cancel_waits_for_actual_execution_stop(client, repo, services):
    data = services
    store = ExecutionStore(repo)
    run = start(store, data)
    path = f'/api/mobile/projects/lease-testing/tasks/{data["task"]["id"]}'
    response = client.post(path + '/cancel', headers=data['headers'], json={})
    assert response.status_code == 200, response.text
    assert response.json()['cancel_requested'] is True and response.json()['stopped'] is False
    assert client.get(path).json()['task']['cancel_requested'] is True
    store.acknowledge_stopped(data['task']['id'], data['device']['id'], run['generation'], run['run_key'])
    assert client.get(path).json()['task']['cancel_requested'] is False
    assert client.post(path + '/cancel', headers=data['headers'], json={}).json()['stopped'] is True
