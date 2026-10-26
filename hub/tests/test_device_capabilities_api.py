import json


def login(client):
    response = client.post('/login', data={'username': 'owner', 'password': 'owner-password'}, follow_redirects=False)
    assert response.status_code == 303
    return client.cookies.get('codex_hub_csrf')


def provision(repo, label='device-a', slug='catalog-project'):
    device = repo.create_device(label=label)
    token = repo.create_api_token(device_id=device['id'], name='main')
    project = repo.create_project(slug=slug, name=slug, created_by_device_id=device['id'])
    return device, {'Authorization': f'Bearer {token.raw}'}, project


def catalog(model='gpt-6-astra'):
    return {'execution_backend': 'codex', 'token': 'must-not-persist', 'codex': {
        'source': 'models-cache', 'fetched_at': '2026-09-11T00:00:00Z',
        'models': [{'model': model, 'displayName': model,
                    'supportedReasoningEfforts': [{'reasoningEffort': 'high'}, {'reasoningEffort': 'ultra'}],
                    'defaultReasoningEffort': 'high', 'instructions': 'private text'}]}}


def test_capabilities_require_device_auth_and_catalog_is_project_scoped(client, repo):
    device, headers, project = provision(repo)
    other, other_headers, other_project = provision(repo, 'outside', 'other-project')
    assert client.post('/api/device/capabilities', json=catalog()).status_code == 401
    assert client.post('/api/device/capabilities', headers=headers, json=catalog()).status_code == 200
    endpoint = f"/api/projects/{project['slug']}/models"
    assert client.get(endpoint).status_code == 401
    assert client.get(endpoint, headers=other_headers).status_code == 403
    response = client.get(endpoint, headers=headers)
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert [item['device_id'] for item in response.json()['devices']] == [device['id']]
    assert response.json()['devices'][0]['codex']['models'][0]['model'] == 'gpt-6-astra'


def test_catalog_refresh_is_durable_sanitized_and_owner_visible(client, repo, settings):
    device, headers, project = provision(repo)
    client.post('/api/device/capabilities', headers=headers, json=catalog())
    path = settings.data_dir / 'device-capabilities' / f"{device['id']}.json"
    saved = path.read_text(encoding='utf-8')
    assert 'must-not-persist' not in saved
    assert 'private text' not in saved
    assert json.loads(saved)['received_at']
    client.post('/api/device/capabilities', headers=headers, json=catalog('future-model'))
    login(client)
    result = client.get(f"/api/projects/{project['slug']}/models").json()
    assert result['devices'][0]['codex']['models'][0]['model'] == 'future-model'


def test_corrupt_capabilities_do_not_break_project_picker(client, repo, settings):
    device, headers, project = provision(repo)
    client.post('/api/device/capabilities', headers=headers, json=catalog())
    path = settings.data_dir / 'device-capabilities' / f"{device['id']}.json"
    path.write_bytes(b'\x00{')
    result = client.get(f"/api/projects/{project['slug']}/models", headers=headers)
    assert result.status_code == 200
    assert result.json()['devices'][0]['codex']['models'] == []


def test_entry_rejects_bad_routing_before_saving(client, repo):
    device, headers, project = provision(repo)
    for routing in ('all', {'mode': 'broadcast'}, {'mode': 'selected', 'device_ids': [True]}, {'mode': 'selected', 'device_ids': '1'}):
        result = client.post(f"/api/projects/{project['slug']}/entries", headers=headers,
                             json={'body_markdown': 'bad routing', 'routing': routing})
        assert result.status_code == 422
    assert repo.list_project_entries(project['id']) == []


def test_mobile_form_restricts_execution_and_reply_to_selected_member(client, repo):
    primary, primary_headers, project = provision(repo)
    advisor = repo.create_device(label='advisor')
    repo.add_project_member(project['id'], advisor['id'], 'write')
    advisor_token = repo.create_api_token(device_id=advisor['id'], name='main')
    advisor_headers = {'Authorization': f'Bearer {advisor_token.raw}'}
    csrf = login(client)
    response = client.post(f"/projects/{project['slug']}/entries", data={
        '_csrf': csrf, 'kind': 'message', 'body_markdown': '@device-a do this independently',
        'delivery_mode': 'selected', 'target_device_ids': str(advisor['id']),
    }, headers={'x-requested-with': 'codex-hub-composer'})
    assert response.status_code == 201
    entry = response.json()['entry']
    assert json.loads(entry['metadata_json'])['routing'] == {'mode': 'selected', 'device_ids': [advisor['id']]}
    assert repo.get_mention_for_device(entry['id'], primary['id']) is None
    assert repo.get_mention_for_device(entry['id'], advisor['id'])
    reply = client.post(f"/api/projects/{project['slug']}/entries", headers=advisor_headers, json={
        'body_markdown': '@device-a completed', 'reply_to_entry_id': entry['id'],
    })
    assert reply.status_code == 201
    assert json.loads(reply.json()['entry']['metadata_json'])['routing']['mode'] == 'none'
    assert repo.get_mention_for_device(reply.json()['entry']['id'], primary['id']) is None


def test_task_reply_metadata_cannot_wake_an_unselected_device(client, repo):
    primary, primary_headers, project = provision(repo)
    advisor = repo.create_device(label='task-advisor')
    repo.add_project_member(project['id'], advisor['id'], 'write')
    token = repo.create_api_token(device_id=advisor['id'], name='main')
    task = repo.create_task(project['id'], title='Independent task', assigned_device_id=advisor['id'])
    owner = repo.create_owner_if_missing('owner', 'owner-password')
    entry = repo.create_entry(project['id'], source_label='owner', kind='message', body_markdown='do task',
                              author_user_id=owner['id'], task_id=task['id'],
                              metadata_json=json.dumps({'routing': {'mode': 'selected', 'device_ids': [advisor['id']]}}))
    reply = client.post(f"/api/projects/{project['slug']}/tasks/{task['id']}/entries",
                        headers={'Authorization': f'Bearer {token.raw}'},
                        json={'body_markdown': '@device-a completed', 'reply_to_entry_id': entry['id']})
    assert reply.status_code == 201
    metadata = json.loads(reply.json()['entry']['metadata_json'])
    assert metadata['reply_to_entry_id'] == entry['id']
    assert metadata['routing'] == {'mode': 'none', 'device_ids': []}
    assert repo.get_mention_for_device(reply.json()['entry']['id'], primary['id']) is None
