"""Credential and probe lifecycle checks for the independently reviewed boundary."""
import httpx
import pytest

from codex_bridge.model_service_client import probe_service
from codex_hub.execution_leases import ExecutionStore
from codex_hub.model_policy import ensure_task_choice, save_policy
from test_agents import token_headers
from test_execution_leases import lease_task, start  # noqa: F401
from test_model_services import services, create, config  # noqa: F401


def test_owner_revoked_while_reading_settings_body_cannot_save(client, repo, services, monkeypatch):
    import codex_hub.model_service_routes as routes
    original = routes.object_body
    async def demote_after_read(request):
        data = await original(request)
        repo.connection.execute("UPDATE users SET role='viewer' WHERE username='owner'")
        repo.connection.commit()
        return data
    monkeypatch.setattr(routes, 'object_body', demote_after_read)
    response = client.post(services['path'], headers=services['headers'], json=config(services))
    assert response.status_code == 403
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_model_services').fetchone()[0] == 0


@pytest.mark.parametrize('change,status', [('cancel',409),('revoke',401),('membership',403)])
def test_credential_fetch_rechecks_state_after_request_body(client, repo, settings, services, monkeypatch, change, status):
    import codex_hub.model_service_routes as routes
    data = services
    sid = create(client, data, api_key='isolated-review-secret')
    save_policy(repo.connection, data['worker'], settings.data_dir, mode='manual', model=f'hub-service/{sid}', reasoning_effort='none')
    repo.connection.execute("UPDATE tasks SET execution_route='direct' WHERE id=?", (data['task']['id'],))
    repo.connection.commit()
    task = repo.get_task(data['task']['id'])
    ensure_task_choice(repo, task, settings.data_dir)
    run = start(ExecutionStore(repo), data)
    headers = token_headers(repo, data['device'])
    original = routes.object_body
    async def change_after_read(request):
        body = await original(request)
        if change == 'cancel':
            repo.update_task_status(task['id'], 'cancelled')
        elif change == 'revoke':
            repo.connection.execute('UPDATE api_tokens SET is_active=0 WHERE device_id=?', (data['device']['id'],))
            repo.connection.commit()
        else:
            repo.remove_project_member(data['project']['id'], data['device']['id'])
        return body
    monkeypatch.setattr(routes, 'object_body', change_after_read)
    response = client.post(f'/api/projects/lease-testing/tasks/{task["id"]}/model-service', headers=headers, json={'execution':run})
    assert response.status_code == status
    assert 'isolated-review-secret' not in response.text


@pytest.mark.parametrize('invalid', [{'code':[]}, {'claim_token':'不是令牌'}, {'claim_token':{}}])
def test_malformed_probe_completion_is_a_bounded_conflict(client, repo, services, invalid):
    data = services
    sid = create(client, data)
    data['store'].queue_probe(data['project']['id'], sid)
    job = data['store'].claim_probe(data['device']['id'])
    response = client.post(f'/api/model-services/probes/{job["id"]}/complete', headers=token_headers(repo, data['device']),
        json={'code':'ready','claim_token':job['claim_token'],**invalid})
    assert response.status_code == 409


def test_expired_probe_retries_with_new_token_and_rejects_late_result(client, repo, services):
    data = services
    sid = create(client, data)
    data['store'].queue_probe(data['project']['id'], sid)
    first = data['store'].claim_probe(data['device']['id'])
    repo.connection.execute("UPDATE hub_model_service_probes SET expires_at='2020-01-01T00:00:00+00:00' WHERE id=?", (first['id'],))
    repo.connection.commit()
    with pytest.raises(ValueError):
        data['store'].complete_probe(data['device']['id'], first['id'], {'code':'ready','claim_token':first['claim_token']})
    next_run = data['store'].claim_probe(data['device']['id'])
    assert next_run['id'] == first['id'] and next_run['claim_token'] != first['claim_token']
    with pytest.raises(ValueError):
        data['store'].complete_probe(data['device']['id'], first['id'], {'code':'ready','claim_token':first['claim_token']})
    payload = {'code':'ready','claim_token':next_run['claim_token'],'models':['verified-model']}
    assert data['store'].complete_probe(data['device']['id'], first['id'], payload)['accepted']
    assert data['store'].complete_probe(data['device']['id'], first['id'], payload)['accepted']


def test_removed_device_cannot_publish_a_successful_probe(client, repo, services):
    data = services
    sid = create(client, data)
    data['store'].queue_probe(data['project']['id'], sid)
    job = data['store'].claim_probe(data['device']['id'])
    repo.remove_project_member(data['project']['id'], data['device']['id'])
    with pytest.raises(ValueError, match='权限'):
        data['store'].complete_probe(data['device']['id'], job['id'], {'code':'ready','claim_token':job['claim_token']})
    assert data['store'].list(data['project']['id'])[0]['status'] != 'ready'


def probe_with_response(monkeypatch, response):
    requests = []
    def transport(request):
        requests.append(request)
        if response.is_stream_consumed:
            return httpx.Response(response.status_code, headers=response.headers,
                                  stream=httpx.ByteStream(response.content))
        return response
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: original(transport=httpx.MockTransport(transport), **kwargs))
    result = probe_service({'base_url':'http://127.0.0.1:11434/v1','api_key':'isolated-reflected-key'})
    assert len(requests) == 1 and requests[0].method == 'GET'
    assert requests[0].url.path == '/v1/models'
    return result


def test_probe_filters_oversized_and_reflected_secret_model_ids(monkeypatch):
    result = probe_with_response(monkeypatch, httpx.Response(200, json={'data':[
        {'id':'good-model'}, {'id':'good-model'}, {'id':'bad<script>'}, {'id':'a' * 10000},
        {'id':'prefix-isolated-reflected-key'}, {'id':'other-model'}]}))
    assert result == {'code':'ready','models':['good-model','other-model']}


def test_probe_never_follows_redirect_with_credentials(monkeypatch):
    assert probe_with_response(monkeypatch, httpx.Response(307, headers={'Location':'https://other.example/v1/models'})) == {'code':'http'}


def test_probe_bounds_response_and_keeps_raw_errors_private(monkeypatch):
    assert probe_with_response(monkeypatch, httpx.Response(200, content=b'x' * (1024*1024+1))) == {'code':'invalid'}


def test_probe_trickle_is_stopped_by_wall_budget(monkeypatch):
    import codex_bridge.model_service_client as module
    elapsed = [0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: elapsed[0])
    class Trickle(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(100):
                elapsed[0] += 3
                yield b' '
    assert probe_with_response(monkeypatch, httpx.Response(200, stream=Trickle())) == {'code':'timeout'}
    assert elapsed[0] == 15


def test_probe_rejects_compression_before_reading_an_expanding_body(monkeypatch):
    class MustNotRead(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError('compressed bytes must not be read')
    assert probe_with_response(monkeypatch, httpx.Response(200, headers={'Content-Encoding':'gzip'}, stream=MustNotRead())) == {'code':'invalid'}
