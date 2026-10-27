import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import pytest

from codex_hub.agents import AgentStore
from codex_hub.execution_leases import ExecutionStore, ExecutionConflict
from codex_hub.work_delivery import repair_user_messages, revise_task, dependency_ready
from codex_hub.model_policy import select_model, save_policy
from codex_hub.device_capabilities import save_device_capabilities
from codex_bridge.artifacts import collect_artifacts, publish_artifacts
from codex_bridge.agent_worker import controller_config, run_lane, AgentOnlyHubClient
from codex_bridge.client import HubClient
from test_execution_leases import lease_task, start, RESULT
from test_workspace_attachments import upload_team, post_files, created_tasks
from test_agents import token_headers
from test_workspace_shell import login
from test_model_policy import setup_team
from test_agent_runtime import config_for


def test_text_messages_immediately_persist_and_retry_does_not_duplicate(client,repo,upload_team):
    d=upload_team
    kwargs=dict(files=[],description='继续聊天的纯文字',conversation_id=str(d['conversation_id']))
    first=post_files(client,d,**kwargs)
    assert first.status_code==303
    assert post_files(client,d,**kwargs).status_code==303
    rows=repo.connection.execute("SELECT * FROM entries WHERE body_markdown='继续聊天的纯文字'").fetchall()
    assert len(rows)==1 and rows[0]['author_type']=='user'
    page=client.get(first.headers['location'])
    assert '已收到' in page.text
    assert page.text.count('继续聊天的纯文字')>=1
    assert repair_user_messages(repo,d['project']['id'])==[]


def test_historical_repair_idempotent_and_uses_original_time(client,repo,upload_team):
    d=upload_team
    post_files(client,d,files=[],description='以前没有显示的文字',conversation_id=str(d['conversation_id']))
    repo.connection.execute("DELETE FROM entries WHERE body_markdown='以前没有显示的文字'");repo.connection.commit()
    repaired=repair_user_messages(repo,d['project']['id'])
    assert len(repaired)==1
    row=repo.get_entry(repaired[0])
    assert row['body_markdown']=='以前没有显示的文字' and row['author_type']=='user'
    assert repair_user_messages(repo,d['project']['id'])==[]


def upload_result(client,repo,d,run,*,name='report.md',body=b'# Result\n<script>bad()</script>',key='a'*64,reference='output/report.md'):
    return client.post(f"/api/projects/{d['project']['slug']}/tasks/{d['task']['id']}/artifacts",
        headers=token_headers(repo,d['device']),data={'execution':json.dumps(run),'artifact_key':key,'local_reference':reference},
        files={'file':(name,body,'text/markdown' if name.endswith('.md') else 'image/png')})


@pytest.mark.parametrize('late',[False,True])
def test_result_files_replay_preview_and_atomic_entry_binding(client,repo,settings,lease_task,late):
    d=lease_task; store=ExecutionStore(repo); run=start(store,d)
    result={**RESULT,'body':'[说明](output/report.md)','summary':'[说明](output/report.md)'}
    if late: store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],**result)
    first=upload_result(client,repo,d,run)
    assert first.status_code==200,first.text
    second=upload_result(client,repo,d,run)
    assert second.json()['replayed'] and second.json()['attachment_id']==first.json()['attachment_id']
    if not late: store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],**result)
    entry=repo.connection.execute('SELECT * FROM entries WHERE task_id=?',(d['task']['id'],)).fetchone()
    assert '/attachments/' in entry['body_markdown'] and '/preview)' in entry['body_markdown']
    assert '/attachments/' in repo.get_task(d['task']['id'])['result_summary']
    attachment=repo.connection.execute('SELECT * FROM attachments WHERE id=?',(first.json()['attachment_id'],)).fetchone()
    assert attachment['entry_id']==entry['id']
    assert upload_result(client,repo,d,run,body=b'conflicting bytes').status_code==422
    assert len(list(settings.files_dir.rglob('*.*')))==1
    login(client)
    page=client.get(first.json()['url']+'/preview')
    assert page.status_code==200 and '&lt;script&gt;' in page.text and '<script>bad()' not in page.text
    replay=store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],**result)
    assert replay['replayed']


def test_artifacts_reject_old_execution_wrong_device_and_cancelled_task(client,repo,lease_task):
    d=lease_task; run=start(ExecutionStore(repo),d)
    assert upload_result(client,repo,d,{**run,'run_key':'wrong-key-12345'}).status_code==409
    stranger=repo.create_device('Different computer');repo.add_project_member(d['project']['id'],stranger['id'],'write')
    assert upload_result(client,repo,{**d,'device':stranger},run).status_code==409
    repo.update_task_status(d['task']['id'],'cancelled','user cancelled')
    assert upload_result(client,repo,d,run).status_code==409
    assert repo.connection.execute('SELECT COUNT(*) FROM attachments').fetchone()[0]==0


def controller_source(repo,d):
    target=repo.get_task(d['task']['id'])
    source=repo.create_task(d['project']['id'],'修改要求',assigned_agent_id=d['controller']['id'])
    source=d['agents'].route(source['id'],{'action':'reuse','conversation_id':target['conversation_id'],'reason':'same work'},'codex_cli')
    run=start(ExecutionStore(repo),{**d,'task':source},'controller-revision-001')
    return source,run


def test_isolated_revision_receives_outputs_created_after_revision_was_queued(client,repo,lease_task):
    d=lease_task;store=ExecutionStore(repo)
    repo.connection.execute("UPDATE tasks SET execution_mode='isolated' WHERE id=?",(d['task']['id'],));repo.connection.commit()
    execution=start(store,d)
    source,control_run=controller_source(repo,d)
    amendment=revise_task(repo,source,d['device']['id'],{'target_task_id':d['task']['id'],
        'instruction':'根据成品调整标题','request_key':'isolated-revision-001','execution':control_run})
    next_task=repo.get_task(amendment['next_task_id'])
    assert next_task['execution_mode']=='isolated'
    uploaded=upload_result(client,repo,d,execution)
    assert uploaded.status_code==200
    store.complete(d['task']['id'],d['device']['id'],execution['generation'],execution['run_key'],**RESULT)
    store.acquire(next_task['id'],d['device']['id'],'isolated-revision-run-001')
    assert uploaded.json()['attachment_id'] in {a['id'] for a in repo.list_task_attachments(d['project']['id'],next_task['id'])}


@pytest.mark.parametrize('running',[False,True])
def test_revisions_are_idempotent_wait_until_prior_work_finishes(repo,lease_task,running):
    d=lease_task;store=ExecutionStore(repo)
    target_run=start(store,d) if running else None
    source,run=controller_source(repo,d)
    payload={'target_task_id':d['task']['id'],'instruction':'新的包装不能被挡住','request_key':'revision-once-001','execution':run}
    first=revise_task(repo,source,d['device']['id'],payload)
    assert first==revise_task(repo,source,d['device']['id'],payload)
    next_task=repo.get_task(first['next_task_id'])
    assert '新的包装不能被挡住' in next_task['description']
    if running:
        assert first['mode']=='queued' and next_task['id']!=d['task']['id']
        assert not dependency_ready(repo.connection,next_task)
        assert next_task['id'] not in {t['id'] for t in repo.list_inbox_tasks_for_device(d['device']['id'])}
        with pytest.raises(ExecutionConflict):store.acquire(next_task['id'],d['device']['id'],'too-early-run-001')
        # Subsequent adjustments coalesce into the same unstarted continuation.
        again=revise_task(repo,source,d['device']['id'],{**payload,'instruction':'标题也要端正','request_key':'revision-next-002'})
        assert again['next_task_id']==next_task['id']
        store.complete(d['task']['id'],d['device']['id'],target_run['generation'],target_run['run_key'],**RESULT)
        assert dependency_ready(repo.connection,next_task)
        assert next_task['id'] in {t['id'] for t in repo.list_inbox_tasks_for_device(d['device']['id'])}
        assert store.acquire(next_task['id'],d['device']['id'],'continuation-run-001')['execution']
    else:
        assert first['mode']=='updated' and next_task['id']==d['task']['id']
    with pytest.raises(ValueError):revise_task(repo,source,d['device']['id'],{**payload,'instruction':'conflicting retry'})


def test_artifact_collection_respects_workspace_and_publishes_links(tmp_path):
    p=tmp_path/'output'/'成果.png';p.parent.mkdir();p.write_bytes(b'png')
    body='[成果](output/成果.png)\n<hub-artifacts>[{"path":"output/成果.png"}]</hub-artifacts>'
    cleaned,items=collect_artifacts(body,tmp_path)
    assert len(items)==1 and 'hub-artifacts' not in cleaned
    calls=[]
    client=SimpleNamespace(upload_task_artifact=lambda *args:calls.append(args))
    assert publish_artifacts(client,'test',1,{},tmp_path,body)==cleaned and len(calls)==1
    (tmp_path/'config.json').write_text('{}')
    for reference in ('../outside.md','config.json','output/missing.md'):
        with pytest.raises(ValueError):collect_artifacts('<hub-artifacts>'+json.dumps([{'path':reference}])+'</hub-artifacts>',tmp_path)
    assert collect_artifacts('[remote](https://example.com/test.png)',tmp_path)[1]==[]


def test_same_artifact_absolute_and_relative_links_upload_once(tmp_path):
    path=tmp_path/'output'/'成果.png';path.parent.mkdir();path.write_bytes(b'png')
    absolute=path.as_posix()
    body=f'[绝对路径]({absolute})\n[相对路径](output/成果.png)\n[尖括号](<{absolute}>)\n'
    body+='<hub-artifacts>'+json.dumps([{'path':'output/成果.png'}])+'</hub-artifacts>'
    calls=[]
    client=SimpleNamespace(upload_task_artifact=lambda *args:calls.append(args))
    cleaned=publish_artifacts(client,'test',1,{},tmp_path,body)
    assert len(calls)==1 and calls[0][4]=='output/成果.png'
    assert cleaned.count('(output/成果.png)')==3
    assert absolute not in cleaned


def test_duplicate_historical_artifact_aliases_show_one_card(client,repo,lease_task):
    from starlette.requests import Request
    from codex_hub.workspace import _entry_views
    d=lease_task;store=ExecutionStore(repo);run=start(store,d)
    assert upload_result(client,repo,d,run).status_code==200
    assert upload_result(client,repo,d,run,key='b'*64,reference='/old/report.md').status_code==200
    store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],**RESULT)
    task=repo.get_task(d['task']['id'])
    rows,_=_entry_views(Request({'type':'http','root_path':'','headers':[]}),repo,d['project'],task['conversation_id'],None)
    assert len(rows[0]['attachments'])==1
    assert repo.connection.execute('SELECT COUNT(*) FROM attachments').fetchone()[0]==2


def test_controller_lane_can_reply_while_executor_busy_and_state_is_separate(tmp_path,monkeypatch):
    config=config_for(tmp_path);other=controller_config(config)
    assert other.state_path!=config.state_path and other.prompt_dir!=config.prompt_dir
    assert other.codex_exec_sandbox=='workspace-write' and other.backend_options['agent_network_access']
    assert other.projects['agent-test']!=config.projects['agent-test']
    inbox={'device':{'id':1},'tasks':[{'id':n,'assigned_device_id':1,'assigned_agent_id':n,'assigned_agent_kind':kind,'status':'open'} for n,kind in [(1,'controller'),(2,'worker')]]}
    monkeypatch.setattr(HubClient,'get_inbox',lambda _:inbox)
    a=AgentOnlyHubClient('https://hub.example','test');a.lane='controller'
    b=AgentOnlyHubClient('https://hub.example','test');b.lane='worker'
    assert [t['id'] for t in a.get_inbox()['tasks']]==[1]
    assert [t['id'] for t in b.get_inbox()['tasks']]==[2]
    busy=threading.Event();release=threading.Event();replied=threading.Event();stop=threading.Event()
    def work():busy.set();release.wait(5);return SimpleNamespace(messages=[])
    def chat():replied.set();return SimpleNamespace(messages=[])
    threads=[threading.Thread(target=run_lane,args=(SimpleNamespace(process_once=fn),stop,3)) for fn in (work,chat)]
    try:
        threads[0].start();assert busy.wait(1)
        threads[1].start();assert replied.wait(1) and not release.is_set()
    finally:
        stop.set();release.set()
        for thread in threads:thread.join(5)


@pytest.mark.parametrize('text,effort',[('查看目前进度','medium'),('安排工程师修改界面','high'),('深度审查运行逻辑','xhigh')])
def test_controller_prefers_astra_and_61_is_a_real_candidate(repo,settings,text,effort):
    project,device,store,controller,worker,models=setup_team(repo,settings)
    for row in models:row['supportedReasoningEfforts'].append({'reasoningEffort':'xhigh'})
    models.append({**models[0],'model':'gpt-6.1-sol'})
    save_device_capabilities(settings.data_dir,device['id'],{'codex':{'models':models}})
    choice=select_model(repo.connection,controller,settings.data_dir,task={'description':text})
    assert (choice['model'],choice['reasoning_effort'])==('gpt-6-astra',effort)
    assert select_model(repo.connection,worker,settings.data_dir)['model']=='gpt-6.1-sol'
    save_policy(repo.connection,controller,settings.data_dir,mode='manual',model='gpt-5.6-sol',reasoning_effort='high')
    assert select_model(repo.connection,controller,settings.data_dir,task={'description':text})['model']=='gpt-5.6-sol'


@pytest.mark.parametrize('permanent',[False,True])
def test_failed_artifact_delivery_never_reruns_execution(repo,tmp_path,monkeypatch,permanent):
    from test_execution_recovery import runtime, restart
    from codex_bridge.client import HubClientError
    monkeypatch.setattr('codex_bridge.model_allocator.allocate_task_model',lambda *args:{})
    runner,client,executor=runtime(repo,tmp_path)
    artifact=tmp_path/'project'/'result.md';artifact.write_text('verified output')
    executor.summary='[Result](result.md)\n<hub-artifacts>[{"path":"result.md"}]</hub-artifacts>\n<hub-reflection>'+json.dumps({'summary':'done','worked':'tested','failed':'','next_time':'check output'})+'</hub-reflection>'
    attempts=[]
    def upload(*args):
        attempts.append(1)
        if permanent:raise HubClientError('invalid upload',status_code=422)
        if len(attempts)==1:raise HubClientError('connection lost')
    client.upload_task_artifact=upload
    runner.process_once()
    if not permanent:
        assert repo.get_task(client.task_id)['status']=='in_progress'
        runner=restart(runner,client,executor);runner.process_once()
    task=repo.get_task(client.task_id)
    assert task['status']==('blocked' if permanent else 'review')
    assert '<hub-artifacts>' not in task['result_summary']
    assert len(executor.calls)==1
    assert repo.connection.execute('SELECT COUNT(*) FROM entries WHERE task_id=?',(client.task_id,)).fetchone()[0]==1
