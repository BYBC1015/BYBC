"""Health checks use isolated metadata only; no worker/model is started."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from codex_hub.agents import AgentStore
from codex_hub.db import connect_database
from codex_hub.maintenance import MaintenanceStore
from codex_hub.maintenance_health import observe_health, pending_signals
from codex_hub.repository import Repository
from test_maintenance import maintenance_team


NOW = datetime(2026, 10, 29, 15, 30, tzinfo=timezone.utc)


def execution(repo, team, *, blocked=False, delay=150, from_start=1, at=NOW,
              route='chat', room=None, agent=None, version_delta=0, task=None):
    agent = agent or (team['engineer'] if blocked else team['controller'])
    task = task or repo.create_task(team['project']['id'], 'PRIVATE TASK TITLE',
        'PRIVATE MESSAGE BODY', assigned_agent_id=agent['id'])
    state = 'blocked' if blocked else 'review'
    repo.update_task_status(task['id'],state,'PRIVATE RESULT')
    task = repo.get_task(task['id'])
    with repo.connection:
        repo.connection.execute('UPDATE tasks SET execution_route=?,conversation_id=? WHERE id=?', (route,room,task['id']))
        started = at.timestamp()-600
        eid = repo.connection.execute('''INSERT INTO hub_task_executions
            (task_id,generation,run_key,device_id,state,task_version,lease_until,created_at,started_at,completed_at)
            VALUES (?,1,?,?,'completed',?,?,?,?,?)''', (task['id'],f'health-fixture-{task["id"]}',
            team['connection']['id'],task['status_version']+version_delta,started+1000,
            started-2,started,started+200)).lastrowid
        repo.connection.execute('UPDATE hub_execution_timings SET from_start=?,first_public_at=? WHERE execution_id=?',
            (from_start,None if delay is None else started+delay,eid))
    return {'task_id':task['id'], 'execution_id':eid}


def repeated(repo, team, **kwargs):
    return [execution(repo,team,**kwargs) for _ in range(3)]


def samples(repo):
    return [dict(r) for r in repo.connection.execute('SELECT * FROM hub_maintenance_health_observations')]


def test_queued_weekly_receives_evidence_once_without_losing_requirements(repo, maintenance_team):
    team = maintenance_team
    store, pid = team['maintenance'],team['project']['id']
    run = store.queue_week(pid,at=NOW)
    original = repo.get_task(run['task_id'])['description']
    source = repeated(repo,team,blocked=True,route='project')
    before = repo.connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
    memories = [dict(r) for r in repo.connection.execute('SELECT * FROM hub_agent_experiences ORDER BY id')]
    result = store.queue_health(pid,at=NOW)
    assert result['merged'] and result['task_id']==run['task_id']
    description = repo.get_task(run['task_id'])['description']
    assert description.startswith(original) and len(description)<=16000
    assert all(f'../tasks/{s["task_id"]}' in description for s in source)
    assert '实际发布版本/回滚记录' in description and '自己写的“已修好”不是验证' in description
    evidence = store.run(run['id'])['health_signals']
    assert evidence[0]['sample_count']==3
    assert 'PRIVATE' not in json.dumps(evidence)
    assert all(s['run_id']==run['id'] for s in samples(repo))
    assert store.queue_health(pid,at=NOW+timedelta(minutes=1))['deferred']=='no_repeated_signal'
    assert repo.get_task(run['task_id'])['description']==description
    assert repo.connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==before
    assert [dict(r) for r in repo.connection.execute('SELECT * FROM hub_agent_experiences ORDER BY id')]==memories
    assert all(repo.get_task(s['task_id'])['status']=='blocked' for s in source)


@pytest.mark.parametrize('interrupt', [None, 15, -10, 'partial'])
def test_unknown_fast_clock_or_partial_breaks_consecutive_slow_series(repo, maintenance_team, interrupt):
    team = maintenance_team
    repeated(repo,team)
    execution(repo,team,delay=180 if interrupt=='partial' else interrupt,
              from_start=0 if interrupt=='partial' else 1)
    execution(repo,team)
    with repo.connection:
        assert observe_health(repo.connection,team['project']['id'],now=NOW.timestamp())==0
    assert not samples(repo)


def test_true_controller_public_delay_creates_one_daily_diagnostic_with_metadata_only(repo, maintenance_team):
    team = maintenance_team
    source = repeated(repo,team)
    run = team['maintenance'].queue_health(team['project']['id'],at=NOW)
    assert '-health-' in run['week_key']
    assert repo.get_task(run['task_id'])['assigned_agent_id']==team['engineer']['id']
    signal = run['health_signals'][0]
    assert signal['kind']=='slow_first_public'
    assert {e['task_id'] for e in signal['evidence']}=={s['task_id'] for s in source}
    assert all(e['start_to_first_public_seconds']==150 and e['first_public_source']=='progress' for e in signal['evidence'])
    assert 'PRIVATE' not in json.dumps(signal)


def test_blocked_requires_three_distinct_recent_fenced_tasks(repo, maintenance_team):
    team = maintenance_team
    execution(repo,team,blocked=True)
    execution(repo,team,blocked=True)
    execution(repo,team,blocked=True,version_delta=-1)
    execution(repo,team,blocked=True,at=NOW-timedelta(days=2))
    assert team['maintenance'].queue_health(team['project']['id'],at=NOW)['deferred']=='no_repeated_signal'
    assert not samples(repo)


@pytest.mark.parametrize('state', ['claimed','in_progress','blocked','acquired','legacy_claim'])
def test_never_changes_started_or_claimed_maintenance_prompt(repo, maintenance_team, state):
    team = maintenance_team
    store, pid = team['maintenance'],team['project']['id']
    run = store.queue_week(pid,at=NOW)
    if state=='legacy_claim':
        with repo.connection:
            repo.connection.execute('UPDATE tasks SET claimed_by_device_id=? WHERE id=?',(team['connection']['id'],run['task_id']))
    elif state=='acquired':
        from codex_hub.execution_leases import ExecutionStore
        ExecutionStore(repo).acquire(run['task_id'],team['connection']['id'],'health-reserved-run')
        # Even an old bridge that puts the task back to open cannot unfreeze it.
        with repo.connection:
            repo.connection.execute("UPDATE tasks SET status='open' WHERE id=?",(run['task_id'],))
    else:
        repo.update_task_status(run['task_id'],state,'already owned')
    before = repo.get_task(run['task_id'])['description']
    repeated(repo,team,blocked=True)
    result = store.queue_health(pid,at=NOW)
    assert result['deferred']=='maintenance_in_progress'
    assert repo.get_task(run['task_id'])['description']==before
    assert len(samples(repo))==3 and all(s['run_id'] is None for s in samples(repo))
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_maintenance_runs').fetchone()[0]==1


def test_review_suppresses_same_cause_but_different_issue_is_not_starved(repo, maintenance_team):
    team = maintenance_team
    store, pid = team['maintenance'],team['project']['id']
    weekly = store.queue_week(pid,at=NOW)
    repeated(repo,team,blocked=True,route='project')
    store.queue_health(pid,at=NOW)
    repo.update_task_status(weekly['task_id'],'review','self reported fixed is not verification')
    repeated(repo,team,blocked=True,route='project')
    waiting = store.queue_health(pid,at=NOW)
    assert waiting['deferred']=='awaiting_verification'
    assert waiting['pending_signals'][0]['awaiting_verification_task_id']==weekly['task_id']
    repeated(repo,team,blocked=True,route='direct')
    diagnostic = store.queue_health(pid,at=NOW)
    assert diagnostic['task_id']!=weekly['task_id']
    assert len(diagnostic['health_signals'])==1
    assert diagnostic['health_signals'][0]['cause_key'].endswith(':direct:completed')
    repo.update_task_status(diagnostic['task_id'],'done','finished')
    repeated(repo,team)
    assert store.queue_health(pid,at=NOW)['deferred']=='daily_limit'
    tomorrow = NOW+timedelta(hours=1)
    another = store.queue_health(pid,at=tomorrow)
    assert another['task_id']!=diagnostic['task_id']
    assert len(another['health_signals'])==1 and another['health_signals'][0]['kind']=='slow_first_public'


def test_maintenance_and_its_conversation_do_not_form_a_diagnostic_loop(repo, maintenance_team):
    team = maintenance_team
    store,pid = team['maintenance'],team['project']['id']
    weekly = store.queue_week(pid,at=NOW)
    task = repo.get_task(weekly['task_id'])
    execution(repo,team,blocked=True,task=task,room=task['conversation_id'])
    repeated(repo,team,blocked=True,room=task['conversation_id'])
    repeated(repo,team,room=task['conversation_id'])
    assert store.queue_health(pid,at=NOW)['deferred']=='no_repeated_signal'
    assert not samples(repo)


@pytest.mark.parametrize('mode', ['disabled','unbound','paused'])
def test_health_respects_configuration_and_real_engineer_binding(repo, maintenance_team, mode):
    team = maintenance_team
    store,pid = team['maintenance'],team['project']['id']
    repeated(repo,team,blocked=True)
    if mode=='disabled':
        store.configure(pid,team['engineer']['id'],False,team['user']['id'])
        assert store.queue_health(pid,at=NOW)['disabled']
    else:
        agent=team['engineer']
        team['store'].update(pid,agent['id'],job=agent['job'],instructions='',
            device_id=None if mode=='unbound' else agent['device_id'],active=mode!='paused')
        assert store.queue_health(pid,at=NOW)['deferred']=='engineer_unavailable'
        assert len(samples(repo))==3
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_maintenance_runs').fetchone()[0]==0


def test_concurrent_health_checks_dispatch_once(settings, repo, maintenance_team):
    team = maintenance_team
    repeated(repo,team,blocked=True)
    barrier=threading.Barrier(2)
    def check():
        db=connect_database(settings.database_path)
        try:
            barrier.wait(timeout=5)
            return MaintenanceStore(Repository(db)).queue_health(team['project']['id'],at=NOW)
        finally:
            db.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _:check(),range(2)))
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_maintenance_runs').fetchone()[0]==1
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_agent_dispatches').fetchone()[0]==1


def test_lost_diagnostic_ack_recovers_frozen_snapshot_even_across_week_boundary(repo, maintenance_team, monkeypatch):
    team = maintenance_team
    store,pid=team['maintenance'],team['project']['id']
    repeated(repo,team,blocked=True)
    dispatch=AgentStore.dispatch
    def lose_ack(self,*args,**kwargs):
        dispatch(self,*args,**kwargs)
        raise RuntimeError('synthetic acknowledgement lost')
    monkeypatch.setattr(AgentStore,'dispatch',lose_ack)
    with pytest.raises(RuntimeError):
        store.queue_health(pid,at=NOW)
    repeated(repo,team)
    monkeypatch.setattr(AgentStore,'dispatch',dispatch)
    recovered=store.queue_week(pid,at=NOW+timedelta(days=7))
    assert recovered['task_id'] and recovered['carryover']
    assert len(recovered['health_signals'])==1
    assert recovered['health_signals'][0]['kind']=='repeated_blocked'
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_agent_dispatches').fetchone()[0]==1


def test_health_evidence_is_visible_through_existing_scoped_maintenance_api(client,repo,maintenance_team):
    from test_agents import token_headers
    team=maintenance_team
    repeated(repo,team,blocked=True)
    run=team['maintenance'].queue_health(team['project']['id'],at=NOW)
    response=client.get(f'/api/projects/workspace-team/maintenance/tasks/{run["task_id"]}',
        headers=token_headers(repo,team['connection']))
    assert response.status_code==200
    assert response.json()['run']['health_signals'][0]['sample_count']==3
    assert 'PRIVATE' not in json.dumps(response.json()['run']['health_signals'])


def test_missing_telemetry_is_unknown_and_future_clock_is_not_slow(repo,maintenance_team):
    team=maintenance_team
    repeated(repo,team,delay=900)
    assert team['maintenance'].queue_health(team['project']['id'],at=NOW)['deferred']=='no_repeated_signal'
    with repo.connection:
        repo.connection.execute('DELETE FROM hub_execution_timings')
        observe_health(repo.connection,team['project']['id'],now=NOW.timestamp())
    assert not samples(repo)


def test_full_existing_prompt_is_not_truncated_or_replaced(repo,maintenance_team):
    team=maintenance_team
    store,pid=team['maintenance'],team['project']['id']
    run=store.queue_week(pid,at=NOW)
    original='Original requirement, preserve exactly. '*410
    with repo.connection:
        repo.connection.execute('UPDATE tasks SET description=? WHERE id=?',(original,run['task_id']))
    repeated(repo,team,blocked=True)
    result=store.queue_health(pid,at=NOW)
    assert result['deferred']=='prompt_capacity'
    assert repo.get_task(run['task_id'])['description']==original
    assert all(s['run_id'] is None for s in samples(repo))
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_maintenance_runs').fetchone()[0]==1


def test_next_week_can_collect_waiting_verification_without_extra_diagnostic(repo,maintenance_team):
    team=maintenance_team
    store,pid=team['maintenance'],team['project']['id']
    old=store.queue_week(pid,at=NOW)
    repeated(repo,team,blocked=True)
    store.queue_health(pid,at=NOW)
    repo.update_task_status(old['task_id'],'review','waiting for actual verification')
    later=NOW+timedelta(days=7)
    weekly=store.queue_week(pid,at=later)
    repeated(repo,team,blocked=True,at=later)
    result=store.queue_health(pid,at=later)
    assert result['merged'] and result['task_id']==weekly['task_id']
    assert '当前等待核验' in repo.get_task(weekly['task_id'])['description']
    assert store.run(weekly['id'])['health_signals'][0]['awaiting_verification_task_id']==old['task_id']
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_maintenance_health_daily').fetchone()[0]==0


@pytest.mark.parametrize('unbound', [False,True])
def test_hourly_cli_runs_real_health_collection(settings,repo,maintenance_team,monkeypatch,capsys,unbound):
    from codex_hub.config import Settings
    from codex_hub.maintenance import main
    team=maintenance_team
    at=datetime.now(timezone.utc)
    repeated(repo,team,blocked=True,at=at)
    if unbound:
        engineer=team['engineer']
        team['store'].update(team['project']['id'],engineer['id'],job=engineer['job'],instructions='',device_id=None,active=True)
    monkeypatch.setattr(Settings,'from_env',staticmethod(lambda:settings))
    assert main(['--once','--project','workspace-team'])==int(unbound)
    result=json.loads(capsys.readouterr().out)
    assert len(samples(repo))==3
    if unbound:
        assert result[0]['health']['deferred']=='engineer_unavailable'
        assert all(s['run_id'] is None for s in samples(repo))
    else:
        assert result[0]['health']['merged']
        assert repo.connection.execute('SELECT COUNT(*) FROM hub_maintenance_runs').fetchone()[0]==1
    assert 'PRIVATE' not in json.dumps(result)
