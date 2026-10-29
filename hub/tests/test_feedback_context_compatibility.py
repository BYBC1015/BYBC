"""Historical and automatic feedback remains readable without rewriting evidence."""
import json

import pytest

from test_agents import token_headers
from test_maintenance import maintenance_team, report  # noqa: F401


CONTEXTS = [
    pytest.param('{"browser":{"platform":"Win32"},"server":{},"execution":{}}', id='missing-viewport'),
    pytest.param('{"source":"maintenance-health","work":{"task_id":32}}', id='automatic-feedback'),
    pytest.param('{}', id='missing-sections'),
    pytest.param('null', id='null-context'),
    pytest.param('[]', id='list-context'),
    pytest.param('"legacy note"', id='text-context'),
    pytest.param('12', id='number-context'),
    pytest.param('{bad-json', id='malformed-json'),
    pytest.param('{"browser":null,"server":[],"execution":"legacy","work":false}', id='invalid-sections'),
    pytest.param('{"browser":{"viewport":null},"execution":null}', id='null-viewport'),
    pytest.param('{"browser":{"viewport":[1440,900]}}', id='list-viewport'),
    pytest.param('{"browser":{"viewport":{"width":[],"height":{}},"platform":{}},"server":{"url":[],"port":false},"execution":{"device_label":[]}}', id='invalid-leaf-types'),
    pytest.param('{"browser":{"viewport":{"width":-1,"height":true}},"server":{"port":70000}}', id='invalid-dimensions'),
]


@pytest.mark.parametrize('raw_context', CONTEXTS)
def test_feedback_history_context_is_readable_in_ui_api_and_maintenance_snapshot(
        client, repo, maintenance_team, raw_context):
    data = maintenance_team
    row = report(repo, data)
    repo.connection.execute('UPDATE hub_feedback SET context_json=? WHERE id=?', (raw_context, row['id']))
    repo.connection.commit()
    before = dict(repo.connection.execute('SELECT * FROM hub_feedback WHERE id=?', (row['id'],)).fetchone())

    page = client.get('/projects/workspace-team/feedback')
    assert page.status_code == 200
    assert row['title'] in page.text and row['body'] in page.text
    assert f'id="feedback-{row["id"]}"' in page.text and '未知' in page.text
    assert client.get('/projects/workspace-team/maintenance').status_code == 200
    record = data['maintenance'].feedback(data['project']['id'], data['user']['id'])[0]
    context = record['context']
    assert all(isinstance(context[key], dict) for key in ('browser', 'server', 'execution', 'work'))
    assert context['browser']['viewport'] == {'width':None, 'height':None}
    assert record['events'][0]['note'] == '反馈已记录，等待工程师统一排查。'
    if 'maintenance-health' in raw_context:
        assert context['source'] == 'maintenance-health' and context['work']['task_id'] == 32

    api = client.get(f'/api/projects/workspace-team/maintenance/feedback/{row["id"]}',
                     headers=token_headers(repo, data['connection']))
    assert api.status_code == 200 and api.json()['context'] == context
    snapshot = data['maintenance']._snapshot(data['project']['id'])
    assert len(snapshot['feedback']) == 1
    assert snapshot['feedback'][0]['id'] == row['id']
    assert snapshot['feedback'][0]['body'] == row['body']
    after = dict(repo.connection.execute('SELECT * FROM hub_feedback WHERE id=?', (row['id'],)).fetchone())
    assert after == before  # Read compatibility must not rewrite user evidence.


def test_feedback_context_keeps_valid_provenance_and_does_not_mutate_source(client, repo, maintenance_team):
    data = maintenance_team
    original = {
        'source':'maintenance-health',
        'server':{'url':'https://example.test/hub', 'host':'example.test', 'port':443, 'app_version':'2.9.4'},
        'browser':{'reported_page_url':'https://example.test/hub/chat', 'platform':'Win32',
                   'user_agent':'Synthetic valid browser', 'viewport':{'width':1440, 'height':900}},
        'execution':{'device_id':data['connection']['id'], 'device_label':'BYBC-YOLO',
                     'machine_name':'BYBC-YOLO', 'note':'Reported by executor'},
        'work':{'task_id':32},
    }
    raw = json.dumps(original, ensure_ascii=False)
    row = report(repo, data)
    repo.connection.execute('UPDATE hub_feedback SET context_json=? WHERE id=?', (raw, row['id']))
    repo.connection.commit()
    assert data['maintenance'].feedback(data['project']['id'], data['user']['id'])[0]['context'] == original
    page = client.get('/projects/workspace-team/feedback')
    assert page.status_code == 200 and '1440 × 900' in page.text
    assert 'example.test : 443' in page.text and 'BYBC-YOLO' in page.text
    assert repo.connection.execute('SELECT context_json FROM hub_feedback WHERE id=?', (row['id'],)).fetchone()[0] == raw
