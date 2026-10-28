import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_hub.conversation_context import conversation_brief, conversation_files, inherit_attachments
from codex_hub.execution_leases import ExecutionStore
from test_agents import team, token_headers


def upload(name='meeting.pdf', data=b'%PDF-meeting', size=None):
    return dict(original_filename=name, stored_relpath='inputs/'+name, content_type='application/pdf',
                byte_size=len(data) if size is None else size, sha256=hashlib.sha256(data).hexdigest())


def user_turn(repo, team, *, text, room=None, files=None, suffix='first'):
    project, device, _, store, controller, *_ = team
    user = repo.create_owner_if_missing('continuity-owner', 'test-only-password')
    return store.dispatch(repo, project['id'], agent_ids=[controller['id']], title=text[:100],
                          description=text, request_key='continuity-'+suffix, conversation_id=room,
                          user_id=user['id'], attachments=files or [])['tasks'][0]


def start(repo, task, device):
    store = ExecutionStore(repo)
    execution = store.acquire(task['id'], device['id'], 'continuity-run-'+str(task['id']))['execution']
    return store.start(task['id'], device['id'], execution['generation'], execution['run_key'])['execution']


def test_original_brief_user_corrections_and_documents_survive_recent_task_eviction(repo, team):
    first = user_turn(repo, team, text='请理解会议资料，准备延展四个大屏设计', files=[upload()])
    repo.update_task_status(first['id'], 'review', '总体交付：美丽、宜居、创新、韧性四张大屏。文明和智慧是两套参考。')
    room = first['conversation_id']
    for index in range(12):
        current = user_turn(repo, team, text=f'追加第{index}条：所有大屏不出现具体城市地标', room=room, suffix=str(index))
    future = user_turn(repo, team, text='未来轮次不能混入正在处理的用户指令', room=room, suffix='future')
    context = conversation_brief(repo.connection, current)
    assert context['initial_requirement']['task_id'] == first['id']
    assert '四张大屏' in context['initial_requirement']['prior_understanding']
    assert context['user_updates'][-1]['task_id'] == current['id'] - 1
    assert '不出现具体城市地标' in context['user_updates'][-1]['user_request']
    assert '未来轮次' not in json.dumps(context, ensure_ascii=False)
    assert context['reference_files'][0]['filename'] == 'meeting.pdf'
    assert not context['reference_files'][0]['attached_to_current_task']
    assert repo.list_task_attachments(current['project_id'], current['id']) == []
    prompt = team[3].prompt(current)
    assert '四张大屏' in prompt and 'inherit_attachments' in prompt


def test_context_size_is_bounded_without_losing_source_anchor(repo, team):
    first = user_turn(repo, team, text='原始目标'+('需求长句' * 3000), files=[upload()])
    repo.update_task_status(first['id'], 'review', '四个设计交付'+('理解长句' * 3000))
    for index in range(12):
        current = user_turn(repo, team, text='最新修订'+('修订长句' * 3000), room=first['conversation_id'], suffix=str(index))
    result = conversation_brief(repo.connection, current)
    assert len(json.dumps(result, ensure_ascii=False, separators=(',', ':'))) <= 12100
    assert result['initial_requirement']['user_request'].startswith('原始目标')
    assert result['initial_requirement']['prior_understanding'].startswith('四个设计交付')


def test_catalog_and_inheritance_are_room_scoped_fenced_and_idempotent(client, repo, team):
    project, device, _, store, controller, *_ = team
    first = user_turn(repo, team, text='原始要求', files=[upload()])
    repo.update_task_status(first['id'], 'review', '已读取原始资料')
    other = user_turn(repo, team, text='另一对话保密资料', files=[upload('private.pdf')], suffix='other')
    current = user_turn(repo, team, text='继续当前总体目标', room=first['conversation_id'], suffix='followup')
    own = repo.list_task_attachments(project['id'], first['id'])[0]['id']
    foreign = repo.list_task_attachments(project['id'], other['id'])[0]['id']
    base = f"/api/projects/{project['slug']}/tasks/{current['id']}"
    headers = token_headers(repo, device)
    catalog = client.get(base+'/reference-files', headers=headers)
    assert catalog.status_code == 200
    assert [row['id'] for row in catalog.json()['reference_files']] == [own]
    assert client.post(base+'/inherit-attachments', headers=headers, json={'attachment_ids': [own]}).status_code == 409
    execution = start(repo, current, device)
    for _ in range(2):
        response = client.post(base+'/inherit-attachments', headers=headers,
                               json={'attachment_ids': [own], 'execution': execution})
        assert response.status_code == 200
        assert len(response.json()['attachments']) == 1
        assert response.json()['attachments'][0]['source_task_id'] == first['id']
    assert client.post(base+'/inherit-attachments', headers=headers,
                       json={'attachment_ids': [foreign], 'execution': execution}).status_code == 422
    stale = dict(execution, generation=execution['generation'] + 1)
    assert client.post(base+'/inherit-attachments', headers=headers,
                       json={'attachment_ids': [own], 'execution': stale}).status_code == 409
    assert len(repo.list_task_attachments(project['id'], current['id'])) == 1


def test_explicitly_inherited_inputs_remain_delegatable_and_empty_selection_stays_empty(repo, team):
    project, device, _, store, controller, designer, _ = team
    source = user_turn(repo, team, text='理解完整资料', files=[upload()])
    repo.update_task_status(source['id'], 'review', '已读取原始资料')
    current = user_turn(repo, team, text='现在开始制作四张', room=source['conversation_id'], suffix='followup')
    original = repo.list_task_attachments(project['id'], source['id'])[0]['id']
    execution = start(repo, current, device)
    inherit_attachments(repo, current, device['id'], {'attachment_ids': [original], 'execution': execution})
    for index, selection in enumerate(([original], [])):
        delegated = store.dispatch(repo, project['id'], agent_ids=[designer['id']], title='Produce '+str(index),
            description='Only the requested deliverable', request_key='continuity-delegate-'+str(index),
            conversation_id=current['conversation_id'], device_id=device['id'], source_task_id=current['id'],
            attachment_ids=selection, execution=execution)['tasks'][0]
        assert [row['id'] for row in repo.list_task_attachments(project['id'], delegated['id'])] == selection


def test_readonly_followup_cannot_mutate_input_bindings(repo, team):
    project, device, *_ = team
    source = user_turn(repo, team, text='最初资料', files=[upload()])
    followup = user_turn(repo, team, text='还需要什么资料吗', room=source['conversation_id'], suffix='followup')
    repo.connection.execute('UPDATE tasks SET controller_read_only=1 WHERE id=?', (followup['id'],))
    repo.connection.commit()
    followup = repo.get_task(followup['id'])
    execution = start(repo, followup, device)
    source_id = repo.list_task_attachments(project['id'], source['id'])[0]['id']
    with pytest.raises(ValueError, match='只读'):
        inherit_attachments(repo, followup, device['id'], {'attachment_ids': [source_id], 'execution': execution})
    assert not repo.list_task_attachments(project['id'], followup['id'])


def test_inheritance_budget_failure_is_atomic(repo, team):
    project, device, *_ = team
    source = user_turn(repo, team, text='原始资料', files=[upload(str(i)+'.pdf', size=40*1024*1024) for i in range(3)])
    repo.update_task_status(source['id'], 'review', '已读取原始资料')
    followup = user_turn(repo, team, text='开始制作', room=source['conversation_id'], suffix='followup')
    execution = start(repo, followup, device)
    ids = [row['id'] for row in repo.list_task_attachments(project['id'], source['id'])]
    with pytest.raises(ValueError, match='100 MiB'):
        inherit_attachments(repo, followup, device['id'], {'attachment_ids': ids, 'execution': execution})
    assert not repo.list_task_attachments(project['id'], followup['id'])


@pytest.mark.parametrize('legacy', [False, True])
def test_helper_downloads_original_into_actual_controller_conversation_workspace(repo, team, tmp_path, monkeypatch, legacy):
    from codex_bridge.agent_runtime import run_agent_request
    from codex_bridge.agent_worker import controller_config
    from codex_bridge.task_pool import task_config
    from test_agent_runtime import config_for
    project, device, _, store, *_ = team
    payload = b'%PDF-real-source-bytes'
    source = user_turn(repo, team, text='理解会议资料', files=[upload(data=payload)])
    repo.update_task_status(source['id'], 'review', '已读取原始资料')
    current = user_turn(repo, team, text='接续制作', room=source['conversation_id'], suffix='followup')
    execution = start(repo, current, device)
    attachment_id = repo.list_task_attachments(project['id'], source['id'])[0]['id']
    config = config_for(tmp_path)
    config.token = 'private-continuity-credential-should-not-appear'
    config.projects = {project['slug']: config.projects['agent-test']}
    derived = task_config(controller_config(config), dict(current, project_slug=project['slug'], assigned_agent_kind='controller'))
    workspace = controller_config(config).projects[project['slug']] if legacy else derived.projects[project['slug']]
    workspace.mkdir(parents=True)
    monkeypatch.chdir(workspace)
    monkeypatch.setattr('codex_bridge.config.load_config', lambda _: config)
    monkeypatch.setattr('codex_bridge.hub_reply.select_runtime_config', lambda value, _: value)
    for suffix, value in [('TASK_ID', current['id']), ('PROJECT', project['slug']),
                          ('GENERATION', execution['generation']), ('RUN_KEY', execution['run_key'])]:
        monkeypatch.setenv('CODEX_HUB_EXECUTION_'+suffix, str(value))
    class Client:
        def __init__(self, *args, **kwargs): pass
        def get_agent_context(self, *args): return store.context(repo.get_task(current['id']))
        def _json(self, method, path, data): return inherit_attachments(repo, current, device['id'], data)
        def get_task_attachments(self, *args): return {'attachments': repo.list_task_attachments(project['id'], current['id'])}
        def download_task_attachment(self, slug, file_id, target, **kwargs): target.write_bytes(payload)
    monkeypatch.setattr('codex_bridge.client.HubClient', Client)
    args = SimpleNamespace(config=tmp_path/'config.json', binding_id='', project=project['slug'], task_id=current['id'],
                           request_json=json.dumps({'action':'inherit_attachments','attachment_ids':[attachment_id]}))
    result = run_agent_request(args)
    local = Path(result['files'][0]['local_path'])
    assert workspace.resolve() in local.parents
    assert local.read_bytes() == payload
    assert '不代表已经阅读' in result['note']
    assert config.token not in json.dumps(result)
