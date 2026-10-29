"""Real progress/complete lifecycle preserves timing without retaining content."""
import pytest

from codex_hub.execution_leases import ExecutionStore, ExecutionConflict
from codex_hub.execution_progress import write_progress
from codex_hub.execution_telemetry import execution_latency, initialize_execution_telemetry
from test_execution_leases import lease_task, start, RESULT
from test_agents import token_headers


def fixture_clock(repo, lease_task):
    repo.connection.execute("UPDATE tasks SET created_at='2026-10-29T00:00:00+00:00' WHERE id=?",(lease_task['task']['id'],))
    repo.connection.commit()
    clock=[1793232010.0]
    store=ExecutionStore(repo,clock=lambda:clock[0],lease_seconds=300)
    run=start(store,lease_task)
    return clock,store,run


def send(store,d,run,sequence,kind,text):
    return write_progress(store,d['task']['id'],d['device']['id'],{
        'generation':run['generation'],'run_key':run['run_key'],
        'sequence':sequence,'kind':kind,'text':text})


def test_first_reply_timing_survives_completion_without_status_becoming_reply(repo,lease_task):
    d=lease_task;clock,store,run=fixture_clock(repo,d)
    clock[0]+=3;send(store,d,run,1,'status','已开始处理')
    clock[0]+=7;send(store,d,run,2,'assistant','已核对：四屏仍沿用两套母版。')
    clock[0]+=9;send(store,d,run,3,'status','正在执行工具')
    clock[0]+=1;send(store,d,run,4,'assistant','已核对：四屏仍沿用两套母版。接着处理修订。')
    clock[0]+=10
    store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],**RESULT)
    assert not repo.connection.execute('SELECT * FROM hub_execution_progress').fetchall()
    item=execution_latency(repo.connection,d['project']['id'])[0]
    assert item['timing_coverage']=='complete' and item['first_public_source']=='progress'
    assert item['submit_to_acquire_seconds']==10
    assert item['submit_to_first_public_seconds']==20 and item['start_to_first_public_seconds']==10
    assert item['execution_seconds']==30 and item['last_public_at']==1793232030
    assert set(dict(repo.connection.execute('SELECT * FROM hub_execution_timings').fetchone()))=={
        'execution_id','from_start','first_activity_at','last_activity_at','first_public_at','last_public_at'}


def test_replays_rejected_progress_and_new_generations_cannot_rewrite_first_response(repo,lease_task):
    d=lease_task;clock,store,run=fixture_clock(repo,d)
    clock[0]+=3;send(store,d,run,1,'assistant','首次答复')
    before=execution_latency(repo.connection,d['project']['id'])[0]
    clock[0]+=7;assert send(store,d,run,1,'assistant','首次答复')['replayed']
    assert execution_latency(repo.connection,d['project']['id'])[0]==before
    with pytest.raises(ExecutionConflict):send(store,d,run,1,'assistant','冲突正文')
    store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],**RESULT)
    with pytest.raises(ExecutionConflict):send(store,d,run,2,'assistant','首次答复迟到')
    repo.update_task_status(d['task']['id'],'open','Explicit continuation')
    newer=start(store,d,'telemetry-next-generation')
    clock[0]+=5;send(store,d,newer,1,'assistant','新一代答复')
    records=execution_latency(repo.connection,d['project']['id'])
    assert [r['generation'] for r in records]==[2,1]
    assert records[1]['first_public_at']==before['first_public_at']
    assert records[0]['first_public_at']!=records[1]['first_public_at']


def test_no_streamed_text_uses_real_result_and_preupgrade_data_remains_unknown(repo,lease_task):
    d=lease_task;clock,store,run=fixture_clock(repo,d)
    send(store,d,run,1,'status','已开始处理')
    send(store,d,run,2,'assistant',' \n\t')
    assert execution_latency(repo.connection,d['project']['id'])[0]['first_public_source'] is None
    clock[0]+=10;store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],**RESULT)
    item=execution_latency(repo.connection,d['project']['id'])[0]
    assert item['first_public_source']=='completed_result' and item['submit_to_first_public_seconds']==20
    repo.connection.execute('DELETE FROM hub_execution_timings');repo.connection.commit()
    initialize_execution_telemetry(repo.connection)
    item=execution_latency(repo.connection,d['project']['id'])[0]
    assert item['timing_coverage']=='partial_or_unavailable'
    assert item['submit_to_first_public_seconds'] is None


def test_late_instrumentation_is_partial_and_reports_are_project_scoped(repo,lease_task):
    d=lease_task;clock,store,run=fixture_clock(repo,d)
    repo.connection.execute('DELETE FROM hub_execution_timings');repo.connection.commit()
    send(store,d,run,1,'assistant','观察开始后的公开答复')
    item=execution_latency(repo.connection,d['project']['id'])[0]
    assert item['first_public_at'] and item['timing_coverage']=='partial_or_unavailable'
    assert item['submit_to_first_public_seconds'] is None
    other=repo.create_project('latency-other','Other',created_by_device_id=d['device']['id'])
    assert execution_latency(repo.connection,other['id'])==[]


def test_clock_rollback_is_not_reported_as_instant_response(repo,lease_task):
    d=lease_task;clock,store,run=fixture_clock(repo,d)
    clock[0]-=20;send(store,d,run,1,'assistant','时钟变化后的答复')
    item=execution_latency(repo.connection,d['project']['id'])[0]
    assert item['submit_to_first_public_seconds'] is None and item['start_to_first_public_seconds'] is None


def test_diagnostics_require_project_access_and_never_return_message_text(client,repo,lease_task):
    d=lease_task;clock,store,run=fixture_clock(repo,d)
    send(store,d,run,1,'assistant','Private production details must not enter diagnostics')
    path=f"/api/projects/{d['project']['slug']}/execution-timings"
    assert client.get(path).status_code in {401,403}
    other=repo.create_device('unrelated')
    assert client.get(path,headers=token_headers(repo,other)).status_code==403
    repo.add_project_member(d['project']['id'],other['id'],'read')
    response=client.get(path+'?limit=1',headers=token_headers(repo,other))
    assert response.status_code==200 and len(response.json()['timings'])==1
    assert 'Private production details' not in response.text and run['run_key'] not in response.text
