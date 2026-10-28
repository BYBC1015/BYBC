"""Public drafts are fenced, bounded, ephemeral and do not create transcript entries."""
import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import codex_hub.execution_progress as progress
from codex_hub.db import connect_database
from codex_hub.execution_leases import ExecutionConflict,ExecutionStore
from codex_hub.repository import Repository
from codex_hub.workspace import _task_views
from test_agents import token_headers
from test_execution_leases import lease_task,start,RESULT
from test_workspace_shell import login,workspace_contexts


def running(repo,data):
    store=ExecutionStore(repo);run=start(store,data)
    return store,run


def post(client,repo,data,run,sequence=1,kind='assistant',text='正在整理结果',**extra):
    return client.post(f"/api/projects/{data['project']['slug']}/tasks/{data['task']['id']}/execution/progress",
        headers=token_headers(repo,data['device']),json={
            'generation':run['generation'],'run_key':run['run_key'],'sequence':sequence,'kind':kind,'text':text,**extra})


def test_progress_append_replay_and_stale_packets_never_rewind(client,repo,lease_task):
    d=lease_task;_,run=running(repo,d)
    before=repo.connection.execute('SELECT COUNT(*) FROM entries').fetchone()[0]
    assert post(client,repo,d,run,text='第一段').json()['accepted']
    original=dict(repo.connection.execute('SELECT * FROM hub_execution_progress').fetchone())
    assert post(client,repo,d,run,text='第一段').json()['replayed']
    assert dict(repo.connection.execute('SELECT * FROM hub_execution_progress').fetchone())==original
    version=progress.project_event_version(repo.connection,d['project']['id'])
    progress.write_progress(ExecutionStore(repo),d['task']['id'],d['device']['id'],{
        'generation':run['generation'],'run_key':run['run_key'],'sequence':1,'kind':'assistant','text':'第一段'})
    assert progress.project_event_version(repo.connection,d['project']['id'])==version
    assert post(client,repo,d,run,text='修改旧段落').status_code==409
    assert post(client,repo,d,run,2,text='第一段\n第二段').status_code==200
    assert post(client,repo,d,run,3,'status','正在处理图片').status_code==200
    stale=post(client,repo,d,run,1,text='第一段').json()
    assert stale['ignored'] and not stale['accepted'] and stale['sequence']==3
    visible=progress.active_progress(repo.connection,d['project']['id'])
    assert len(visible)==1 and visible[0]['assistant_text']=='第一段\n第二段'
    assert visible[0]['status_text']=='正在处理图片'
    assert repo.connection.execute('SELECT COUNT(*) FROM entries').fetchone()[0]==before
    assert 'run_key' not in visible[0] and 'device_id' not in visible[0]


@pytest.mark.parametrize('kind,text,extra',[
    ('assistant','x'*24001,{}),('status','x'*301,{}),('reasoning','private',{}),
    ('tool','raw command',{}),('assistant','hello',{'analysis':'private'}),
    ('assistant','<think>private</think>',{}),('assistant','<hub-reflection>private',{}),
    ('assistant',None,{}),('status',{},{}),
])
def test_invalid_progress_never_enters_storage(client,repo,lease_task,kind,text,extra):
    d=lease_task;_,run=running(repo,d)
    assert post(client,repo,d,run,kind=kind,text=text,**extra).status_code==422
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_execution_progress').fetchone()[0]==0


def test_cumulative_assistant_is_bounded_but_complete_large_item_allowed(client,repo,lease_task):
    d=lease_task;_,run=running(repo,d)
    assert post(client,repo,d,run,text='x'*20000).status_code==200
    assert post(client,repo,d,run,2,text='x'*24000).status_code==200
    assert post(client,repo,d,run,3,text='x'*23999+'y').status_code==409
    assert post(client,repo,d,run,3,text='x'*24001).status_code==422


@pytest.mark.parametrize('change',['cancel','version','lease','device','generation','key'])
def test_late_progress_is_rejected_and_stale_draft_hidden(client,repo,lease_task,change):
    d=lease_task;_,run=running(repo,d)
    assert post(client,repo,d,run,text='Draft').status_code==200
    if change=='cancel':repo.update_task_status(d['task']['id'],'cancelled','cancel')
    elif change=='version':
        repo.connection.execute('UPDATE tasks SET status_version=status_version+1 WHERE id=?',(d['task']['id'],));repo.connection.commit()
    elif change=='lease':
        repo.connection.execute('UPDATE hub_task_executions SET lease_until=0 WHERE task_id=?',(d['task']['id'],));repo.connection.commit()
    elif change=='device':
        other=repo.create_device('Other device');repo.add_project_member(d['project']['id'],other['id'],'write')
        repo.connection.execute('UPDATE tasks SET assigned_device_id=? WHERE id=?',(other['id'],d['task']['id']));repo.connection.commit()
    elif change=='generation':run={**run,'generation':run['generation']+1}
    else:run={**run,'run_key':'different-execution-key'}
    assert post(client,repo,d,run,2,text='Draft more').status_code==409
    if change not in {'generation','key'}:
        assert not progress.active_progress(repo.connection,d['project']['id'])


def test_other_project_device_cannot_send_progress(client,repo,lease_task):
    d=lease_task;_,run=running(repo,d)
    other=repo.create_device('Intruder')
    response=client.post(f"/api/projects/{d['project']['slug']}/tasks/{d['task']['id']}/execution/progress",
        headers=token_headers(repo,other),json={**run,'sequence':1,'kind':'assistant','text':'Wrong device'})
    assert response.status_code==403
    repo.add_project_member(d['project']['id'],other['id'],'write')
    response=client.post(f"/api/projects/{d['project']['slug']}/tasks/{d['task']['id']}/execution/progress",
        headers=token_headers(repo,other),json={k:v for k,v in {**run,'sequence':1,'kind':'assistant','text':'Wrong device'}.items()
                                            if k in {'generation','run_key','sequence','kind','text'}})
    assert response.status_code==409


def test_complete_and_new_generation_clear_progress(client,repo,lease_task):
    d=lease_task;store,run=running(repo,d)
    assert post(client,repo,d,run,text='Draft').status_code==200
    store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],**RESULT)
    assert not progress.active_progress(repo.connection,d['project']['id'])
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_execution_progress').fetchone()[0]==0
    assert post(client,repo,d,run,2,text='Draft late').status_code==409
    repo.update_task_status(d['task']['id'],'open','Continue')
    next_run=store.acquire(d['task']['id'],d['device']['id'],'next-project-execution')['execution']
    next_run=store.start(d['task']['id'],d['device']['id'],next_run['generation'],next_run['run_key'])['execution']
    assert post(client,repo,d,next_run,text='New draft').status_code==200
    assert progress.active_progress(repo.connection,d['project']['id'])[0]['generation']==next_run['generation']


def test_completion_and_late_progress_are_serialized_across_connections(client,repo,lease_task,monkeypatch):
    d=lease_task;_,run=running(repo,d)
    assert post(client,repo,d,run,text='Draft').status_code==200
    cleared=threading.Event();commit=threading.Event();writer_started=threading.Event()
    original=progress.clear_progress
    def held_clear(db,task_id):
        original(db,task_id);cleared.set()
        assert commit.wait(5), 'test did not release the completion transaction'
    monkeypatch.setattr(progress,'clear_progress',held_clear)
    def finish():
        db=connect_database(client.app.state.settings.database_path)
        try:
            return ExecutionStore(Repository(db)).complete(d['task']['id'],d['device']['id'],
                run['generation'],run['run_key'],**RESULT)
        finally:db.close()
    def late_write():
        db=connect_database(client.app.state.settings.database_path)
        try:
            writer_started.set()
            return progress.write_progress(ExecutionStore(Repository(db)),d['task']['id'],d['device']['id'],{
                'generation':run['generation'],'run_key':run['run_key'],
                'sequence':2,'kind':'assistant','text':'Draft late'})
        finally:db.close()
    with ThreadPoolExecutor(max_workers=2) as executor:
        completion=executor.submit(finish)
        try:
            assert cleared.wait(5)
            # Other readers see the previous complete snapshot while the result
            # transaction is open, never a half-published final answer.
            assert progress.active_progress(repo.connection,d['project']['id'])[0]['assistant_text']=='Draft'
            assert repo.get_task(d['task']['id'])['status']=='in_progress'
            late=executor.submit(late_write)
            assert writer_started.wait(5)
        finally:commit.set()
        assert completion.result(timeout=5)['accepted']
        with pytest.raises(ExecutionConflict):late.result(timeout=5)
    assert not progress.active_progress(repo.connection,d['project']['id'])
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_execution_progress').fetchone()[0]==0


def test_failed_final_publication_rolls_back_draft_removal(client,repo,lease_task,monkeypatch):
    d=lease_task;store,run=running(repo,d)
    assert post(client,repo,d,run,text='Draft').status_code==200
    def fail(*args,**kwargs):raise RuntimeError('injected publication failure')
    monkeypatch.setattr(repo,'create_entry',fail)
    with pytest.raises(RuntimeError,match='publication failure'):
        store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],**RESULT)
    assert repo.get_task(d['task']['id'])['status']=='in_progress'
    assert progress.active_progress(repo.connection,d['project']['id'])[0]['assistant_text']=='Draft'


def test_workspace_exposes_chat_progress_without_task_card_and_etag_changes(client,repo,lease_task,workspace_contexts):
    d=lease_task
    repo.connection.execute("UPDATE tasks SET execution_route='chat' WHERE id=?",(d['task']['id'],));repo.connection.commit()
    _,run=running(repo,d);login(client)
    room=repo.get_task(d['task']['id'])['conversation_id'];url=f"/projects/{d['project']['slug']}/conversations/{room}"
    before=client.get(url)
    assert post(client,repo,d,run,text='Answer in progress').status_code==200
    after=client.get(url,headers={'If-None-Match':before.headers['etag']})
    assert after.status_code==200
    context=workspace_contexts[-1]
    assert not context['tasks']
    assert context['conversation_progress'][0]['assistant_text']=='Answer in progress'
    assert context['conversation_progress'][0]['task_id']==d['task']['id']
    task=_task_views(repo,d['project']['id'],task_ids=[d['task']['id']])[0]
    assert task['execution_progress']['sequence']==1
    assert context['workspace_events_url'].endswith('/events') and context['workspace_event_version']


def test_project_event_counter_tracks_changes_only_in_affected_project(repo,lease_task):
    d=lease_task;project=d['project']['id'];progress.initialize_progress_schema(repo.connection)
    other=repo.create_project('other-event-project','Other',created_by_device_id=d['device']['id'])
    initial=progress.project_event_version(repo.connection,project)
    repo.create_task(other['id'],'Unrelated work')
    assert progress.project_event_version(repo.connection,project)==initial
    repo.create_entry(project,'User','message','Visible update',author_device_id=d['device']['id'])
    assert progress.project_event_version(repo.connection,project)!=initial
    initial=progress.project_event_version(repo.connection,project)
    repo.connection.execute('UPDATE devices SET last_agent_seen_at=? WHERE id=?',('2026-10-29T00:00:00+00:00',d['device']['id']));repo.connection.commit()
    assert progress.project_event_version(repo.connection,project)!=initial
    progress.initialize_progress_schema(repo.connection)
    assert repo.connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok'


@pytest.fixture
def short_stream(monkeypatch):
    monkeypatch.setattr(progress,'SSE_MAX_SECONDS',.04)
    monkeypatch.setattr(progress,'SSE_POLL_SECONDS',.005)
    monkeypatch.setattr(progress,'SSE_HEARTBEAT_SECONDS',.01)


def test_sse_auth_baseline_headers_and_reconnect_without_content(client,repo,lease_task,short_stream):
    d=lease_task;path=f"/projects/{d['project']['slug']}/events"
    assert client.get(path,follow_redirects=False).status_code in {401,303}
    login(client)
    response=client.get(path)
    assert response.status_code==200 and response.headers['content-type'].startswith('text/event-stream')
    assert response.headers['x-accel-buffering']=='no' and 'content-encoding' not in response.headers
    assert 'retry: 1000' in response.text and 'event: workspace' in response.text and ': heartbeat' in response.text
    data=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    assert data and set(data[0])=={'version'}
    assert d['task']['title'] not in response.text and d['device']['label'] not in response.text
    reconnect=client.get(path,headers={'Last-Event-ID':data[-1]['version']})
    assert 'event: workspace' not in reconnect.text and 'heartbeat' in reconnect.text
    assert not client.app.state.workspace_stream_limiter.counts


def test_stream_rechecks_session_revocation_and_releases_quota(client,repo,lease_task,monkeypatch):
    d=lease_task;login(client)
    token=client.cookies.get('codex_hub_session');user=repo.get_session_user(token)
    monkeypatch.setattr(progress,'SSE_POLL_SECONDS',.001)
    released=[]
    class Request:
        async def is_disconnected(self):return False
    async def consume():
        stream=progress.workspace_event_stream(Request(),database_path=client.app.state.settings.database_path,
            session_token=token,user_id=user['id'],project_id=d['project']['id'],release=lambda:released.append(True))
        assert 'retry:' in await anext(stream)
        assert 'event: workspace' in await anext(stream)
        repo.delete_session(token)
        assert 'session-expired' in await anext(stream)
        with pytest.raises(StopAsyncIteration):await anext(stream)
    asyncio.run(consume())
    assert released==[True]


def test_event_snapshot_stops_cleanly_if_project_was_removed(client,repo):
    login(client)
    token=client.cookies.get('codex_hub_session');user=repo.get_session_user(token)
    project=repo.create_project('removed-stream-project','Temporary stream')
    args=(client.app.state.settings.database_path,token,user['id'],project['id'])
    assert progress.event_snapshot(*args)
    repo.connection.execute('DELETE FROM projects WHERE id=?',(project['id'],));repo.connection.commit()
    assert progress.event_snapshot(*args) is None


def test_connection_limits_allow_multiple_projects_and_release():
    limiter=progress.StreamLimiter();one=[limiter.acquire(1,10) for _ in range(3)]
    assert all(one) and limiter.acquire(1,10) is None
    different=limiter.acquire(1,11);assert different
    one[0]();one[0]()
    replacement=limiter.acquire(1,10);assert replacement
    for release in one[1:]+[different,replacement]:release()
    assert not limiter.counts
