"""Fast follow-ups retain identity while fenced writes stay serialized."""
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from codex_bridge.agent_worker import controller_config, filter_agent_inbox
from codex_bridge.client import HubClient
from codex_bridge.runner import BridgeRunner
from codex_bridge.task_pool import DurableTaskPool, task_config
from codex_hub.controller_followups import is_read_only_followup, allows_read_only_followup
from codex_hub.db import initialize_schema
from codex_hub.execution_leases import ExecutionStore, ExecutionConflict
from test_agents import token_headers
from test_agent_runtime import config_for
from test_execution_leases import lease_task, RESULT
from test_task_pool import ApiClient, GatedExecutor, until


@pytest.mark.parametrize('text', ['进度怎么样了？', '现在做好了吗', '你还需要什么吗',
    '能解释一下刚才的安排吗？', '还需要我提供什么资料？', '我还需要补充哪些信息', '目前状态如何'])
def test_only_complete_status_and_material_questions_use_fast_lane(text):
    assert is_read_only_followup(text)


@pytest.mark.parametrize('text', ['改成蓝色', '你还需要什么吗，顺便开始修改', '进度怎么样了？删除旧版',
    '现在暂停', '取消任务', '需要我补充一个执行器', '帮我总结一下然后继续做', '还需要什么权限就自动补齐',
    '完成了吗\n把它重新做一遍', '还需要修改什么吗', '你i还需要什么吗', '解释当前的安排并开始修改'])
def test_work_and_mixed_requests_never_become_read_only(text):
    assert not is_read_only_followup(text)


def test_attached_files_and_non_user_work_do_not_use_question_shortcut():
    data=dict(agent={'kind':'controller'},route='chat',description='还需要什么资料吗',
              attachments=[],user_id=1,source_task_id=None)
    assert allows_read_only_followup(**data)
    for changes in ({'attachments':[{'id':1}]},{'agent':{'kind':'worker'}},
                    {'user_id':None},{'source_task_id':8},{'route':'project'}):
        assert not allows_read_only_followup(**{**data,**changes})


def test_controller_turns_share_only_their_own_conversation_directory(tmp_path):
    config=controller_config(config_for(tmp_path))
    first={'id':1,'project_slug':'agent-test','assigned_agent_kind':'controller','conversation_id':10}
    previous=task_config(config,first)
    following=task_config(config,{**first,'id':2})
    other=task_config(config,{**first,'id':3,'conversation_id':11})
    readonly=task_config(config,{**first,'id':4,'controller_read_only':1})
    assert previous.projects==following.projects
    assert previous.state_path!=following.state_path
    assert previous.projects!=other.projects!=readonly.projects
    assert previous.backend_options['project_session_root']==following.backend_options['project_session_root']


def test_readonly_cli_retains_selected_model_but_disables_writing_and_local_mcp(tmp_path):
    from codex_bridge.codex_exec import CodexExecRunner
    from test_codex_exec import FakeProcessRunner
    cli=tmp_path/'codex.cmd';cli.write_text('@echo off')
    config=controller_config(replace(config_for(tmp_path),codex_cli_path=cli,codex_exec_ignore_user_config=False))
    config=task_config(config,{'id':1,'project_slug':'agent-test','assigned_agent_kind':'controller',
        'conversation_id':10,'controller_read_only':1})
    workspace=config.projects['agent-test'];workspace.mkdir(parents=True)
    prompt=config.prompt_dir/'prompt.md';prompt.parent.mkdir(parents=True);prompt.write_text('status only')
    process=FakeProcessRunner()
    result=CodexExecRunner(config,process).run('agent-test',workspace,prompt,'status only',
        execution_preferences={'model':'gpt-6-astra','reasoning_effort':'high'})
    assert result.ok
    command=process.calls[0][0]
    assert command[command.index('--sandbox')+1]=='read-only'
    assert '--ignore-user-config' in command
    assert command[command.index('--model')+1]=='gpt-6-astra'
    assert 'model_reasoning_effort="high"' in command


def dispatch(repo, d, text, *, room=None, key=None, attachments=None, user=True):
    # create_app bootstraps the owner; request users are validated by the route.
    owner=repo.connection.execute('SELECT id FROM users ORDER BY id LIMIT 1').fetchone()
    return d['agents'].dispatch(repo,d['project']['id'],agent_ids=[d['controller']['id']],
        title=text,description=text,request_key=key or ('test-followup-'+str(time.time_ns())),
        conversation_id=room,user_id=owner['id'] if owner and user else None,
        attachments=attachments)['tasks'][0]


def test_dispatch_keeps_controller_identity_and_trusts_no_client_flag(client,repo,lease_task):
    d=lease_task
    readonly=dispatch(repo,d,'你还需要什么吗')
    normal=dispatch(repo,d,'改成蓝色',room=readonly['conversation_id'])
    device_message=dispatch(repo,d,'你还需要什么吗',room=readonly['conversation_id'],user=False)
    assert readonly['controller_read_only']==1
    assert normal['controller_read_only']==device_message['controller_read_only']==0
    assert readonly['assigned_agent_id']==normal['assigned_agent_id']==d['controller']['id']
    assert '即时只读追问' in d['agents'].prompt(readonly)
    assert '不需要等待它完成后再回答' in d['agents'].prompt(readonly)


def test_additive_migration_preserves_existing_work_and_defaults_normal(repo,lease_task):
    original=repo.get_task(lease_task['task']['id'])
    repo.connection.execute('ALTER TABLE tasks DROP COLUMN controller_read_only')
    repo.connection.commit()
    initialize_schema(repo.connection)
    initialize_schema(repo.connection)
    migrated=repo.get_task(original['id'])
    assert migrated['controller_read_only']==0
    for key,value in original.items():
        if key!='controller_read_only':assert migrated[key]==value


def test_read_only_api_cannot_delegate_revise_answer_or_change_other_model(client,repo,lease_task):
    d=lease_task;t=dispatch(repo,d,'你还需要什么吗');store=ExecutionStore(repo)
    run=store.acquire(t['id'],d['device']['id'],'readonly-request-key-001')['execution']
    run=store.start(t['id'],d['device']['id'],run['generation'],run['run_key'])['execution']
    prefix=f"/api/projects/{d['project']['slug']}/tasks/{t['id']}"
    headers=token_headers(repo,d['device']);before=repo.list_tasks(d['project']['id'])
    requests=[(prefix+'/delegate',{'assigned_agent_id':d['worker']['id'],'title':'do not create',
        'description':'duplicate','request_key':'readonly-delegate-01'}),
        (prefix+'/revise',{'target_task_id':d['task']['id'],'instruction':'do not edit','request_key':'readonly-revise-001'}),
        (prefix+'/handoff/answer',{'handoff_id':1,'answer':'do not unblock'}),
        (f"/api/projects/{d['project']['slug']}/agents/{d['worker']['id']}/model-policy",
            {'source_task_id':t['id'],'model':'gpt-6-astra','reasoning_effort':'high','reason':'do not change'})]
    for path,payload in requests:
        response=client.post(path,headers=headers,json={**payload,'execution':run})
        assert response.status_code==409,(path,response.text)
        assert '只读追问' in response.json()['detail']
    assert repo.list_tasks(d['project']['id'])==before
    response=client.post(prefix+'/execution/complete',headers=headers,json={**RESULT,**run})
    assert response.status_code==200
    assert repo.get_task(t['id'])['status']=='review'


def test_server_serializes_room_mutations_but_allows_other_rooms_and_readonly(client,repo,lease_task):
    d=lease_task;store=ExecutionStore(repo)
    first=dispatch(repo,d,'安排第一项工作')
    next_work=dispatch(repo,d,'调整第一项工作',room=first['conversation_id'])
    readonly=dispatch(repo,d,'还需要什么资料吗',room=first['conversation_id'])
    other=dispatch(repo,d,'安排另一项工作')
    run=store.acquire(first['id'],d['device']['id'],'room-first-request-0001')['execution']
    store.start(first['id'],d['device']['id'],run['generation'],run['run_key'])
    with pytest.raises(ExecutionConflict,match='资源'):
        store.acquire(next_work['id'],d['device']['id'],'room-next-request-0001')
    for task in (readonly,other):
        assert store.acquire(task['id'],d['device']['id'],'independent-room-'+str(task['id']))['execution']['state']=='prepared'


class ControllerApi(ApiClient):
    def get_inbox(self):
        inbox=filter_agent_inbox(HubClient.get_inbox(self),self.pending_task_ids())
        inbox['tasks']=[t for t in inbox['tasks'] if t.get('assigned_agent_kind')=='controller'
                        and (self.task_id is None or t['id']==self.task_id)]
        return inbox


def test_running_same_room_work_does_not_delay_readonly_followup(client,repo,lease_task,tmp_path,monkeypatch):
    d=lease_task
    first=dispatch(repo,d,'安排实际制作')
    normal=dispatch(repo,d,'再调整制作方向',room=first['conversation_id'])
    other=dispatch(repo,d,'安排另一对话制作')
    readonly=dispatch(repo,d,'你还需要什么吗',room=first['conversation_id'])
    config=controller_config(config_for(tmp_path));config.projects={d['project']['slug']:tmp_path/'controller'}
    monkeypatch.setattr('codex_bridge.model_allocator.allocate_task_model',lambda *a:{})
    monkeypatch.setattr('codex_hub.model_policy.ensure_task_choice',lambda *a:{})
    headers=token_headers(repo,d['device']);made={}
    def factory(task):
        api=ControllerApi(client,headers,task['id'])
        runner=BridgeRunner(task_config(config,task),client=api,executor=GatedExecutor(tmp_path))
        runner._next_capability_report=float('inf');made[task['id']]=runner
        api.pending_task_ids=lambda:{int(r['task_id']) for r in runner.state.task_deliveries.values()}
        return runner
    pool=DurableTaskPool(config,ControllerApi(client,headers),lane='controller',capacity=2,runner_factory=factory)
    all_tasks=(first,normal,other,readonly)
    try:
        until(pool,lambda:all((tmp_path/f'{t["id"]}.started').exists() for t in (first,other,readonly)))
        assert not (tmp_path/f'{normal["id"]}.started').exists()
        assert len({str(made[t['id']].config.state_path) for t in (first,other,readonly)})==3
        read_config=made[readonly['id']].config
        assert read_config.codex_exec_sandbox=='read-only'
        assert read_config.codex_exec_ignore_user_config is True
        assert read_config.projects[d['project']['slug']]!=made[first['id']].config.projects[d['project']['slug']]
        (tmp_path/f'{readonly["id"]}.release').touch()
        until(pool,lambda:repo.get_task(readonly['id'])['status']=='review')
        assert repo.get_task(first['id'])['status']=='in_progress'
        for task in all_tasks:(tmp_path/f'{task["id"]}.release').touch()
        until(pool,lambda:all(repo.get_task(t['id'])['status']=='review' for t in all_tasks))
        assert repo.connection.execute('SELECT COUNT(*) FROM hub_task_executions').fetchone()[0]==4
    finally:
        for task in all_tasks:(tmp_path/f'{task["id"]}.release').touch()
        pool.close()
