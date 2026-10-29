"""Read-only execution views preserve real lifecycle evidence across reconnects."""
import pytest

import codex_hub.execution_progress as progress
from codex_hub.execution_leases import ExecutionConflict, ExecutionStore
from test_execution_leases import lease_task, start, RESULT
from test_workspace_shell import login, workspace_contexts


def _publish(store, data, run, sequence, text, kind='assistant'):
    return progress.write_progress(store, data['task']['id'], data['device']['id'], {
        'generation': run['generation'], 'run_key': run['run_key'],
        'sequence': sequence, 'kind': kind, 'text': text})


def test_started_without_first_packet_is_visible_without_inventing_progress(repo, lease_task, monkeypatch):
    d=lease_task; now=[1800000000.0]
    store=ExecutionStore(repo, clock=lambda:now[0], lease_seconds=30)
    prepared=store.acquire(d['task']['id'],d['device']['id'],'no-first-report-yet')['execution']
    assert not progress.active_progress(repo.connection,d['project']['id'],clock=lambda:now[0],include_expired=True)
    run=store.start(d['task']['id'],d['device']['id'],prepared['generation'],prepared['run_key'])['execution']
    before=repo.connection.total_changes
    row=progress.active_progress(repo.connection,d['project']['id'],clock=lambda:now[0])[0]
    assert row['generation']==run['generation'] and row['sequence']==0
    assert row['execution_state']=='running' and row['execution_contact_valid']
    assert not row['has_public_report'] and row['last_kind']==''
    assert row['updated_at'] is None and row['assistant_text']==row['status_text']==''
    assert row['public_text_version']=='e3b0c44298fc1c14'
    assert row['report_age_label']=='尚未收到'
    assert repo.connection.total_changes==before
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_execution_progress').fetchone()[0]==0
    timing=dict(repo.connection.execute('SELECT * FROM hub_execution_timings').fetchone())
    assert timing['first_activity_at'] is timing['first_public_at'] is None

    # A lease renewal is contact evidence, never a public reply or report.
    now[0]+=10
    store.renew(d['task']['id'],d['device']['id'],run['generation'],run['run_key'])
    renewed=progress.active_progress(repo.connection,d['project']['id'],clock=lambda:now[0])[0]
    assert renewed['lease_until']>row['lease_until'] and renewed['sequence']==0
    assert not renewed['has_public_report'] and renewed['updated_at'] is None
    assert renewed['public_text_version']==row['public_text_version']
    monkeypatch.setattr(progress.time,'time',lambda:now[0])
    initial_version=progress.project_event_version(repo.connection,d['project']['id'])
    now[0]+=15
    assert progress.project_event_version(repo.connection,d['project']['id'])!=initial_version
    now[0]+=31
    assert not progress.active_progress(repo.connection,d['project']['id'],clock=lambda:now[0])
    expired=progress.active_progress(repo.connection,d['project']['id'],clock=lambda:now[0],include_expired=True)[0]
    assert not expired['execution_contact_valid'] and not expired['has_public_report']


@pytest.mark.parametrize('has_report',[False,True])
def test_uncertain_execution_retains_last_evidence_but_rejects_more_text(repo, lease_task, has_report):
    d=lease_task;now=[1000.0];store=ExecutionStore(repo,clock=lambda:now[0],lease_seconds=30)
    run=start(store,d)
    if has_report:_publish(store,d,run,1,'已整理第一部分')
    now[0]+=31
    store.reconcile(d['task']['id'],d['device']['id'])
    assert not progress.active_progress(repo.connection,d['project']['id'],clock=lambda:now[0])
    row=progress.active_progress(repo.connection,d['project']['id'],clock=lambda:now[0],include_expired=True)[0]
    assert row['execution_state']=='uncertain' and not row['execution_contact_valid']
    assert row['has_public_report'] is has_report
    assert row['assistant_text']==('已整理第一部分' if has_report else '')
    assert row['last_kind']==('assistant' if has_report else '')
    with pytest.raises(ExecutionConflict):_publish(store,d,run,2,'已整理第一部分，迟到内容')
    with pytest.raises(ExecutionConflict):store.renew(d['task']['id'],d['device']['id'],run['generation'],run['run_key'])
    # The original receipt remains authoritative and removes the static draft.
    store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],**RESULT)
    assert not progress.active_progress(repo.connection,d['project']['id'],clock=lambda:now[0],include_expired=True)


def test_long_tool_wait_renewals_do_not_reset_public_report_age(repo,lease_task):
    d=lease_task;now=[1000.0];store=ExecutionStore(repo,clock=lambda:now[0],lease_seconds=30)
    run=start(store,d);_publish(store,d,run,1,'等待图像工具返回','status')
    report=dict(repo.connection.execute('SELECT * FROM hub_execution_progress').fetchone())
    timing=dict(repo.connection.execute('SELECT * FROM hub_execution_timings').fetchone())
    for _ in range(6):
        now[0]+=20
        store.renew(d['task']['id'],d['device']['id'],run['generation'],run['run_key'])
    row=progress.active_progress(repo.connection,d['project']['id'],clock=lambda:now[0])[0]
    assert row['execution_contact_valid'] and row['has_public_report'] and row['last_kind']=='status'
    assert row['updated_at']==1000 and row['sequence']==1
    assert row['status_text']=='等待图像工具返回' and row['assistant_text']==''
    assert row['report_age_label']=='1 分 50 秒'  # The shared 15-second display bucket.
    assert dict(repo.connection.execute('SELECT * FROM hub_execution_progress').fetchone())==report
    assert dict(repo.connection.execute('SELECT * FROM hub_execution_timings').fetchone())==timing


def test_parallel_replies_keep_start_order_when_text_changes_and_reconnects(repo, lease_task):
    d=lease_task;now=[1000.0]
    repo.connection.execute("UPDATE tasks SET execution_mode='isolated' WHERE id=?",(d['task']['id'],))
    repo.connection.commit()
    store=ExecutionStore(repo,clock=lambda:now[0]);first=start(store,d)
    second_task=repo.create_task(d['project']['id'],'独立的第二项工作',assigned_agent_id=d['worker']['id'])
    room=repo.get_task(d['task']['id'])['conversation_id']
    repo.connection.execute("UPDATE tasks SET execution_mode='isolated',conversation_id=? WHERE id=?",(room,second_task['id']))
    repo.connection.commit()
    now[0]+=1;other={**d,'task':second_task};second=start(store,other,'second-independent-run')
    _publish(store,d,first,1,'第一项草稿')
    now[0]+=1;_publish(store,other,second,1,'第二项草稿')
    def visible():
        return progress.active_progress(repo.connection,d['project']['id'],conversation_id=room,clock=lambda:now[0])
    expected=[d['task']['id'],second_task['id']]
    initial=visible()
    assert [row['task_id'] for row in initial]==expected
    now[0]+=1;_publish(store,d,first,2,'第一项草稿继续')
    assert [row['task_id'] for row in visible()]==expected
    now[0]+=1;_publish(store,other,second,2,'等待工具返回','status')
    reconnected=visible()
    assert [row['task_id'] for row in reconnected]==expected
    assert reconnected[0]['last_kind']=='assistant' and reconnected[1]['last_kind']=='status'
    assert reconnected[0]['public_text_version']!=initial[0]['public_text_version']
    assert reconnected[1]['public_text_version']==initial[1]['public_text_version']


@pytest.mark.parametrize('change',['cancel','done','version'])
def test_uncertain_evidence_disappears_after_user_changes_work(repo, lease_task, change):
    d=lease_task;now=[1000.0];store=ExecutionStore(repo,clock=lambda:now[0],lease_seconds=30)
    run=start(store,d);_publish(store,d,run,1,'旧的公开内容')
    now[0]+=31;store.reconcile(d['task']['id'],d['device']['id'])
    if change=='version':
        repo.connection.execute('UPDATE tasks SET status_version=status_version+1 WHERE id=?',(d['task']['id'],))
        repo.connection.commit()
    else:repo.update_task_status(d['task']['id'],'cancelled' if change=='cancel' else 'done','用户结束此工作')
    assert not progress.active_progress(repo.connection,d['project']['id'],clock=lambda:now[0],include_expired=True)
    with pytest.raises(ExecutionConflict):_publish(store,d,run,2,'旧的公开内容，不应恢复')


def test_first_packet_transition_does_not_create_fake_entries(client,repo,lease_task,workspace_contexts):
    d=lease_task;store=ExecutionStore(repo);run=start(store,d);login(client)
    room=repo.get_task(d['task']['id'])['conversation_id']
    url=f"/projects/{d['project']['slug']}/conversations/{room}"
    count=repo.connection.execute('SELECT COUNT(*) FROM entries').fetchone()[0]
    initial=client.get(url)
    placeholder=workspace_contexts[-1]['conversation_progress'][0]
    assert placeholder['sequence']==0 and not placeholder['has_public_report']
    _publish(store,d,run,1,'正在核对已有素材','status')
    response=client.get(url,headers={'If-None-Match':initial.headers['etag']})
    assert response.status_code==200
    row=workspace_contexts[-1]['conversation_progress'][0]
    assert row['has_public_report'] and row['last_kind']=='status' and row['sequence']==1
    assert row['view_version']!=placeholder['view_version']
    assert repo.connection.execute('SELECT COUNT(*) FROM entries').fetchone()[0]==count
