"""Feedback provenance, autonomous evidence-based maintenance and retry boundaries."""
import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from codex_hub.agents import AgentStore
from codex_hub.db import connect_database
from codex_hub.device_capabilities import save_device_capabilities
from codex_hub.execution_leases import ExecutionStore
from codex_hub.maintenance import MaintenanceStore, load_diagnostic_summary, maintenance_week, redact_diagnostics
from codex_hub.repository import Repository, utc_now
from codex_hub.security import hash_password
from test_agents import token_headers
from test_workspace_shell import login
from workspace_fixture import seed_workspace


@pytest.fixture
def maintenance_team(client, repo):
    data = seed_workspace(repo, room_messages=0)
    data['csrf'] = login(client)
    data['user'] = repo.get_user_by_username('owner')
    data['maintenance'] = MaintenanceStore(repo)
    return data


def report(repo, data, key='feedback-request-001', **kwargs):
    return data['maintenance'].create_feedback(data['project']['id'], data['user']['id'],
        request_key=key, kind='bug', title='图片加载后丢失阅读位置', body='延迟图片令最后消息移出视口',
        context={'work':{'task_id':data['tasks'][1]['id']},'browser':{'reported_page_url':'http://testserver/projects/workspace-team'}}, **kwargs)


def payload(data, **changes):
    return {'_csrf':data['csrf'],'request_key':'feedback-browser-001','kind':'bug','title':'重现滚动问题',
        'body':'慢速网络打开图片后，当前消息跑出视口。','reproduction':'打开对话，等待图片加载。','expected':'当前消息保持可见。',
        'task_id':str(data['tasks'][1]['id']),'conversation_id':str(data['conversation_id']),
        'agent_id':str(data['engineer']['id']),'page_url':'/projects/workspace-team?secret=private#last',
        'platform':'Win32','viewport_width':'1440','viewport_height':'900',**changes}


def test_feedback_captures_actual_task_device_and_strips_url_secrets(client, repo, settings, maintenance_team):
    data = maintenance_team
    save_device_capabilities(settings.data_dir, data['connection']['id'], {'machine_name':'EXECUTOR-A','execution_backend':'codex'})
    other = repo.create_device(label='后续重新绑定连接')
    repo.add_project_member(data['project']['id'], other['id'], 'write')
    data['store'].update(data['project']['id'],data['engineer']['id'],job='工程师',instructions='',device_id=other['id'],active=True)
    count = len(repo.list_tasks(data['project']['id']))
    target = 'http://testserver:8765/projects/workspace-team/feedback?access_token=private'
    response = client.post(target, data=payload(data), headers={'User-Agent':'Synthetic feedback browser'}, follow_redirects=False)
    assert response.status_code == 303
    row = repo.connection.execute('SELECT * FROM hub_feedback').fetchone()
    context = json.loads(row['context_json'])
    assert context['server']['port'] == 8765 and context['server']['host'] == 'testserver'
    assert 'private' not in row['context_json'] and '?' not in context['server']['url']
    assert context['browser']['viewport'] == {'width':1440,'height':900}
    assert context['browser']['physical_machine_name'] is None
    assert context['execution']['device_id'] == data['connection']['id']
    assert context['execution']['machine_name'] == 'EXECUTOR-A'
    assert context['execution']['binding_source'] == 'task'
    assert len(repo.list_tasks(data['project']['id'])) == count
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_maintenance_runs').fetchone()[0] == 0


def test_feedback_retries_keep_snapshot_and_invalid_request_keeps_draft(client, repo, maintenance_team):
    data = maintenance_team
    path = '/projects/workspace-team/feedback'
    assert client.post(path,data=payload(data),follow_redirects=False).status_code == 303
    original = repo.connection.execute('SELECT context_json FROM hub_feedback').fetchone()[0]
    assert client.post(path,data=payload(data,viewport_width='390'),follow_redirects=False).status_code == 303
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_feedback').fetchone()[0] == 1
    assert repo.connection.execute('SELECT context_json FROM hub_feedback').fetchone()[0] == original
    invalid = client.post(path,data=payload(data,body='修改后的原始内容'),follow_redirects=False)
    assert invalid.status_code == 422 and '修改后的原始内容' in invalid.text
    assert 'data-has-error="true"' in invalid.text
    assert client.post(path,data=payload(data,_csrf='wrong')).status_code == 403
    client.cookies.clear()
    assert client.get(path,follow_redirects=False).status_code in {401,303}


@pytest.mark.parametrize('field', ['task_id','agent_id','conversation_id'])
def test_feedback_rejects_cross_project_context(client, repo, maintenance_team, field):
    data = maintenance_team
    project = repo.create_project(slug='different-project',name='外部项目',created_by_device_id=data['connection']['id'])
    foreign = data['store'].create(project['id'],'外部工程师','实现',device_id=data['connection']['id'])
    task = repo.create_task(project['id'],'外部任务',assigned_agent_id=foreign['id'])
    room = data['store'].route(task['id'],{'action':'new','title':'外部对话','reason':'项目隔离'},'codex_cli')
    values = payload(data,task_id='',agent_id='',conversation_id='')
    values[field] = str({'task_id':task['id'],'agent_id':foreign['id'],'conversation_id':room['conversation_id']}[field])
    assert client.post('/projects/workspace-team/feedback',data=values).status_code == 422
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_feedback').fetchone()[0] == 0


def test_feedback_user_scope_and_maintenance_configuration_permissions(client, repo, maintenance_team):
    data = maintenance_team
    own = report(repo,data)
    current = utc_now()
    repo.connection.execute('INSERT INTO users(username,password_hash,role,created_at,updated_at) VALUES (?,?,?,?,?)',
        ('reporter',hash_password('reporter-password'),'member',current,current))
    repo.connection.commit()
    client.cookies.clear()
    assert client.post('/login',data={'username':'reporter','password':'reporter-password'},follow_redirects=False).status_code == 303
    csrf = client.cookies.get('codex_hub_csrf')
    page = client.get('/projects/workspace-team/feedback')
    assert page.status_code == 200 and own['title'] not in page.text
    assert '<form class="maintenance-config"' not in page.text
    assert client.post(f"/projects/workspace-team/feedback/{own['id']}/status",data={'_csrf':csrf,'status':'new'}).status_code == 422
    for suffix in ['config','queue']:
        assert client.post('/projects/workspace-team/maintenance/'+suffix,data={'_csrf':csrf}).status_code == 403
    assert client.post('/projects/workspace-team/feedback',data=payload(data,_csrf=csrf,task_id='',agent_id='',conversation_id=''),follow_redirects=False).status_code == 303
    assert repo.connection.execute('SELECT COUNT(DISTINCT user_id) FROM hub_feedback').fetchone()[0] == 2


def test_week_boundary_idempotency_carryover_and_no_false_feedback_closure(repo, maintenance_team):
    data = maintenance_team
    store, project = data['maintenance'], data['project']['id']
    feedback = report(repo,data)
    monday = datetime(2026,10,26,9,0,tzinfo=timezone(timedelta(hours=8)))
    assert maintenance_week(monday-timedelta(seconds=1)) == '2026-W43'
    assert maintenance_week(monday) == '2026-W44'
    assert maintenance_week(monday.astimezone(timezone.utc)) == '2026-W44'
    run = store.queue_week(project,at=monday,diagnostic_summary='HTTP 5xx count=2')
    task = repo.get_task(run['task_id'])
    assert task['assigned_agent_id'] == data['engineer']['id'] and task['status'] == 'open'
    assert run['feedback_count'] == 1
    assert 'codex_bridge.maintenance_client' in task['description']
    assert store.queue_week(project,at=monday,diagnostic_summary='Changed later')['task_id'] == task['id']
    assert store.queue_week(project,at=monday+timedelta(days=7))['carryover']
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_maintenance_runs').fetchone()[0] == 1
    repo.update_task_status(task['id'],'done','已完成测试与发布记录')
    store.reconcile(project)
    store.reconcile(project)
    assert store.feedback(project,data['user']['id'])[0]['status'] == 'triaged'
    events = repo.connection.execute('SELECT * FROM hub_maintenance_events WHERE run_id=?',(run['id'],)).fetchall()
    assert [event['task_status'] for event in events] == ['open','done']
    second = store.queue_week(project,at=monday+timedelta(days=7))
    assert second['task_id'] != run['task_id']
    assert store.feedback(project,data['user']['id'])[0]['linked_task_id'] == second['task_id']
    assert feedback['status'] == 'new'


@pytest.mark.parametrize('unavailable', ['paused','unbound','disabled','foreign'])
def test_maintenance_uses_real_available_identity_without_creating_agents(repo, maintenance_team, unavailable):
    data = maintenance_team
    store, project, engineer = data['maintenance'],data['project']['id'],data['engineer']
    before = len(data['store'].list(project))
    if unavailable == 'disabled':
        store.configure(project,engineer['id'],False,data['user']['id'])
        assert store.queue_week(project)['disabled'] is True
    elif unavailable == 'foreign':
        other = repo.create_project(slug='foreign-configuration',name='其他',created_by_device_id=data['connection']['id'])
        agent = data['store'].create(other['id'],'其他工程师','开发',device_id=data['connection']['id'])
        with pytest.raises(ValueError):
            store.configure(project,agent['id'],True,data['user']['id'])
    else:
        data['store'].update(project,engineer['id'],job=engineer['job'],instructions='',
            device_id=None if unavailable=='unbound' else engineer['device_id'],active=unavailable!='paused')
        with pytest.raises(ValueError):
            store.queue_week(project)
    assert len(data['store'].list(project)) == before
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_maintenance_runs').fetchone()[0] == 0


def test_maintenance_crash_after_dispatch_reuses_frozen_snapshot(repo, maintenance_team, monkeypatch):
    data = maintenance_team
    report(repo,data)
    real = AgentStore.dispatch
    def crash_once(self,*args,**kwargs):
        real(self,*args,**kwargs)
        raise RuntimeError('synthetic lost response')
    monkeypatch.setattr(AgentStore,'dispatch',crash_once)
    with pytest.raises(RuntimeError):
        data['maintenance'].queue_week(data['project']['id'])
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_agent_dispatches').fetchone()[0] == 1
    report(repo,data,key='new-feedback-after-crash')
    monkeypatch.setattr(AgentStore,'dispatch',real)
    run = data['maintenance'].queue_week(data['project']['id'])
    assert run['task_id'] and not run['last_error'] and run['feedback_count'] == 1
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_agent_dispatches').fetchone()[0] == 1


def test_concurrent_weekly_checks_dispatch_only_once(settings, repo, maintenance_team):
    project = maintenance_team['project']['id']
    barrier = threading.Barrier(2)
    def enqueue():
        db = connect_database(settings.database_path)
        try:
            barrier.wait(timeout=5)
            return MaintenanceStore(Repository(db)).queue_week(project)['task_id']
        finally:
            db.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _:enqueue(),range(2)))
    assert ids[0] == ids[1]
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_maintenance_runs').fetchone()[0] == 1
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_agent_dispatches').fetchone()[0] == 1


def test_lost_dispatch_at_week_boundary_recovers_prior_run_before_new_work(repo, maintenance_team, monkeypatch):
    data = maintenance_team
    previous = datetime(2026,10,19,9,tzinfo=timezone(timedelta(hours=8)))
    real = AgentStore.dispatch
    def lose_ack(self,*args,**kwargs):
        real(self,*args,**kwargs)
        raise RuntimeError('synthetic scheduler interruption')
    monkeypatch.setattr(AgentStore,'dispatch',lose_ack)
    with pytest.raises(RuntimeError):
        data['maintenance'].queue_week(data['project']['id'],at=previous)
    monkeypatch.setattr(AgentStore,'dispatch',real)
    recovered = data['maintenance'].queue_week(data['project']['id'],at=previous+timedelta(days=7))
    assert recovered['carryover'] and recovered['week_key']=='2026-W43' and recovered['requested_week']=='2026-W44'
    assert recovered['task_id'] and repo.connection.execute('SELECT COUNT(*) FROM hub_agent_dispatches').fetchone()[0] == 1


def test_feedback_engineer_requires_bound_fenced_task_and_verification_evidence(client, repo, maintenance_team):
    data = maintenance_team
    report_id = report(repo,data)['id']
    run = data['maintenance'].queue_week(data['project']['id'])
    headers = token_headers(repo,data['connection'])
    base = '/api/projects/workspace-team/maintenance'
    context = client.get(f"{base}/tasks/{run['task_id']}",headers=headers)
    assert context.status_code == 200 and context.json()['feedback'][0]['id'] == report_id
    path = f'{base}/feedback/{report_id}/status'
    evidence = {'status':'fixed','verification_note':'640 × 360 延迟图片复现，修复后锚点偏移 0 px；相关浏览器回归通过。','note':'修复图片加载锚点','task_id':run['task_id']}
    assert client.post(path,headers=headers,json=evidence).status_code == 409
    execution_store = ExecutionStore(repo)
    prepared = execution_store.acquire(run['task_id'],data['connection']['id'],'maintenance-valid-execution')['execution']
    execution = execution_store.start(run['task_id'],data['connection']['id'],prepared['generation'],prepared['run_key'])['execution']
    evidence['execution'] = {'generation':execution['generation'],'run_key':execution['run_key']}
    assert client.post(path,headers=headers,json={**evidence,'verification_note':''}).status_code == 422
    assert client.post(path,headers=headers,json={**evidence,'status':['fixed']}).status_code == 422
    assert client.post(path,headers=headers,json=evidence).status_code == 200
    current = data['maintenance'].feedback(data['project']['id'],data['user']['id'])[0]
    assert current['status'] == 'fixed' and current['verification_note'] == evidence['verification_note']
    assert current['events'][0]['device_id'] == data['connection']['id']
    assert current['events'][0]['task_id'] == run['task_id']
    assert client.post(f"/projects/workspace-team/feedback/{report_id}/status",data={'_csrf':data['csrf'],'status':'new','note':'仍然有问题'},follow_redirects=False).status_code == 303
    repo.update_task_status(run['task_id'],'cancelled','停止本轮')
    assert client.post(path,headers=headers,json=evidence).status_code == 409
    assert data['maintenance'].feedback(data['project']['id'],data['user']['id'])[0]['status'] == 'new'
    assert client.get(f"{base}/feedback/{report_id}",headers=headers).status_code == 200
    other = repo.create_device(label='无关联执行连接')
    repo.add_project_member(data['project']['id'],other['id'],'write')
    unrelated = token_headers(repo,other)
    assert client.get(f"{base}/feedback/{report_id}",headers=unrelated).status_code == 403
    assert client.get(f"{base}/tasks/{run['task_id']}",headers=unrelated).status_code == 403
    assert client.get(base,headers=unrelated).status_code == 200


def test_diagnostics_are_bounded_redacted_and_fresh(tmp_path, repo, maintenance_team):
    at = datetime.now(timezone.utc)
    path = tmp_path/'diagnostics.json'
    def write(value):
        path.write_text(json.dumps(value),encoding='utf-8')
    write({'collected_at':at.isoformat(),'services':{'hub':{'http_5xx':2}}})
    assert json.loads(load_diagnostic_summary(path,at=at))['services']['hub']['http_5xx'] == 2
    for value in [{'collected_at':(at-timedelta(hours=31)).isoformat()}, {'collected_at':(at+timedelta(minutes=6)).isoformat()}, {'collected_at':'bad'}, {'collected_at':at.isoformat(),'huge':'X'*33000}]:
        write(value)
        assert json.loads(load_diagnostic_summary(path,at=at))['available'] is False
    text = 'password="secret value" Authorization: Bearer privateBearer token=privateToken https://user:privatePass@example.test/error?key=privateQuery'
    redacted = redact_diagnostics(text)
    assert all(secret not in redacted for secret in ['secret value','privateBearer','privateToken','privatePass','privateQuery'])
    data = maintenance_team
    for number in range(20):
        data['maintenance'].create_feedback(data['project']['id'],data['user']['id'],kind='bug',title=f'报告 {number}',body='详'*6000,
            reproduction='复'*3000,expected='期'*2000,context={},request_key=f'large-feedback-{number:02d}')
    run = data['maintenance'].queue_week(data['project']['id'],diagnostic_summary=text+'\n'+'log '*4000)
    task = repo.get_task(run['task_id'])
    assert len(task['description']) <= 16000
    snapshot = json.loads(repo.connection.execute('SELECT snapshot_json FROM hub_maintenance_runs WHERE id=?',(run['id'],)).fetchone()[0])
    assert 0 < len(snapshot['feedback']) < 20
    assert all(item['excerpted'] for item in snapshot['feedback'])
    assert len(snapshot['diagnostic_full']) <= 12000
    assert 'diagnostic_full' not in task['description']
    assert all(secret not in task['description'] for secret in ['privateBearer','privateToken','privatePass','privateQuery'])


def test_maintenance_helper_uses_scoped_api_and_inherited_execution_only(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from codex_bridge import maintenance_client
    import codex_bridge.client
    import codex_bridge.config
    config = SimpleNamespace(hub='http://test.invalid',token='must-not-print',hub_request_timeout_seconds=12,
        hub_get_retry_attempts=1,hub_retry_backoff_seconds=0,hub_source_ip='')
    monkeypatch.setattr(codex_bridge.config,'load_config',lambda _:config)
    calls = []
    class Client:
        def __init__(self,*args,**kwargs): pass
        def _json(self,method,path,data=None):
            calls.append((method,path,data))
            return {'feedback':[{'id':9}],'run':{'task_id':7},'diagnostics':'safe'} if method=='GET' else {'ok':True}
    monkeypatch.setattr(codex_bridge.client,'HubClient',Client)
    file = tmp_path/'request.json'
    args = argparse.Namespace(project='workspace-team',task_id=7,request_file=file,config=tmp_path/'synthetic-config',binding_id=None)
    file.write_text(json.dumps({'action':'list'}),encoding='utf-8')
    assert maintenance_client.run_request(args)['feedback'] == [{'id':9}]
    file.write_text(json.dumps({'action':'get','feedback_id':999}),encoding='utf-8')
    with pytest.raises(ValueError,match='明确关联'):
        maintenance_client.run_request(args)
    file.write_text(json.dumps({'action':'update','feedback_id':9,'status':'fixed','verification_note':'复现修复后两项回归通过'}),encoding='utf-8')
    with pytest.raises(ValueError,match='执行进程'):
        maintenance_client.run_request(args)
    for key,value in {'TASK_ID':'7','PROJECT':'workspace-team','GENERATION':'2','RUN_KEY':'maintenance-client-run-key'}.items():
        monkeypatch.setenv('CODEX_HUB_EXECUTION_'+key,value)
    assert maintenance_client.run_request(args) == {'ok':True}
    assert calls[-1][0] == 'POST' and calls[-1][2]['execution'] == {'generation':2,'run_key':'maintenance-client-run-key'}
    monkeypatch.setenv('CODEX_HUB_EXECUTION_PROJECT','other-project')
    with pytest.raises(ValueError,match='执行进程'):
        maintenance_client.run_request(args)
