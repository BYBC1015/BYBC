"""Scheduling indexes stay small while exact task requirements remain available."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from codex_bridge.agent_inspection import compact_inspection
from codex_bridge.agent_runtime import run_agent_request
from codex_bridge.client import HubClientError
from codex_hub.agents import AgentStore


def test_compact_index_keeps_current_requirements_and_marks_omitted_task_bodies():
    requirements = '智慧→创新、韧性；文明→美丽、宜居。其余区域保持不变。' * 120
    brief = {'initial_goal':requirements, 'latest_requirements':requirements}
    context = {'agent':{'id':1,'kind':'controller','instructions':'Repeated instructions' * 500},
               'task':{'id':1,'description':requirements,'conversation_id':4},
               'conversation_brief':brief, 'handoff':{'question':requirements},
               'attachments':[{'id':9,'original_filename':'reference.png'}],
               'experiences':[{'summary':'Long repeated experience' * 500}]}
    source = {'tasks':[{'id':2,'title':'制作','description':requirements,'status':'blocked',
                       'conversation_id':4,'result_summary':'诊断' * 200}]}
    original = deepcopy((context, source))
    result = compact_inspection(context, [], source)
    assert result['context']['task']['description'] == requirements
    assert result['context']['conversation_brief'] == brief
    assert result['context']['handoff']['question'] == requirements
    assert result['context']['attachments'] == context['attachments']
    assert result['tasks']['tasks'][0]['status'] == 'blocked'
    assert 'description' not in result['tasks']['tasks'][0]
    assert result['tasks']['tasks'][0]['result_summary_truncated'] is True
    assert result['read_more']['task']['action'] == 'inspect'
    assert 'target_task_id' in result['read_more']['task']
    assert result['read_more']['full']['detail'] == 'full'
    assert (context, source) == original


def test_compact_index_prioritizes_current_conversation_and_reports_all_omissions():
    rows = [{'id':i,'title':f'Unrelated {i}','description':'x'*16000,'conversation_id':2,'status':'open'}
            for i in range(10, 110)]
    rows += [{'id':1,'conversation_id':1,'title':'Current owner','status':'in_progress','assigned_agent_id':3},
             {'id':2,'conversation_id':1,'title':'Prior delivery','status':'done','assigned_agent_id':3}]
    result = compact_inspection({'task':{'conversation_id':1}}, [], {'tasks':rows})
    assert [row['id'] for row in result['tasks']['tasks'][:2]] == [1, 2]
    assert result['tasks']['total'] == 102
    assert result['tasks']['returned'] == 60
    assert result['tasks']['same_conversation_total'] == 2
    assert result['tasks']['truncated'] is True
    assert len(json.dumps(result)) < 12000


@pytest.fixture
def inspection_api(repo, client, monkeypatch):
    device = repo.create_device('Isolated inspection fixture')
    project = repo.create_project(slug='inspection-room', name='Inspect', created_by_device_id=device['id'])
    other = repo.create_project(slug='other-room', name='Other', created_by_device_id=device['id'])
    store = AgentStore(repo.connection)
    agent = store.create(project['id'], '总控', '分工', kind='controller', device_id=device['id'])
    source = repo.create_task(project['id'], '当前要求', description='当前要求原文不截断', assigned_agent_id=agent['id'])
    source = store.route(source['id'], {'action':'new','title':'Current','reason':'Fixture'}, 'codex_cli')
    target = repo.create_task(project['id'], '精确验收', description='原始验收要求' * 500)
    foreign = repo.create_task(other['id'], 'Other project requirement')
    entry = repo.create_entry(project['id'], 'Fixture', 'markdown', '原始依据和版本说明' * 500, task_id=target['id'])
    token = repo.create_api_token(device['id'], 'inspection-test').raw
    requests = []

    class LocalClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_agent_context(self, slug, task_id):
            return self._json('GET', f'/api/projects/{slug}/tasks/{task_id}/agent-context')

        def _json(self, method, path):
            requests.append(path)
            response = client.request(method, path, headers={'Authorization':'Bearer '+token})
            if response.status_code >= 400:
                raise HubClientError('Fixture request rejected', status_code=response.status_code)
            return response.json()

    monkeypatch.setattr('codex_bridge.client.HubClient', LocalClient)
    config = SimpleNamespace(hub='http://unused.invalid', token='not-a-production-token',
        hub_request_timeout_seconds=3, hub_get_retry_attempts=1, hub_retry_backoff_seconds=0, hub_source_ip='')
    monkeypatch.setattr('codex_bridge.config.load_config', lambda _:config)
    monkeypatch.setattr('codex_bridge.hub_reply.select_runtime_config', lambda value,_:value)
    def inspect(payload):
        return run_agent_request(SimpleNamespace(config=None,binding_id=None,project=project['slug'],
            task_id=source['id'],request_json=json.dumps({'action':'inspect',**payload})))
    return inspect, requests, source, target, foreign, entry


def test_default_inspect_uses_team_without_redundant_agent_query(inspection_api):
    inspect, calls, source, *_ = inspection_api
    result = inspect({})
    assert result['detail'] == 'compact'
    assert result['context']['task']['description'] == '当前要求原文不截断'
    assert result['agents']['agents'][0]['id'] == source['assigned_agent_id']
    assert len(calls) == 2
    assert not any(path.endswith('/agents') for path in calls)


def test_full_inspect_remains_available_and_target_read_preserves_exact_requirements(inspection_api):
    inspect, calls, _, target, _, entry = inspection_api
    result = inspect({'detail':'full'})
    assert 'instructions' in result['context']['agent']
    assert any(row['description'] == target['description'] for row in result['tasks']['tasks'])
    assert 'conversations' in result['agents']
    calls.clear()
    detailed = inspect({'target_task_id':target['id']})
    assert detailed['detail'] == 'task'
    assert detailed['task']['description'] == target['description']
    assert detailed['entries'][0]['body_markdown'] == entry['body_markdown']
    assert len(calls) == 2
    assert not any(path.endswith('/tasks') for path in calls)


def test_target_lookup_still_enforces_project_boundary(inspection_api):
    inspect, _, _, _, foreign, _ = inspection_api
    with pytest.raises(HubClientError) as failure:
        inspect({'target_task_id':foreign['id']})
    assert failure.value.status_code == 404


@pytest.mark.parametrize('payload', [{'target_task_id':True}, {'target_task_id':0},
    {'target_task_id':'2'}, {'target_task_id':-1}, {'detail':'unknown'}])
def test_invalid_inspect_selector_cannot_change_lookup_scope(inspection_api, payload):
    inspect, calls, *_ = inspection_api
    with pytest.raises(ValueError):
        inspect(payload)
    assert len(calls) == 1  # current identity is verified before any target request
