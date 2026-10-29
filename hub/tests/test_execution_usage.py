import json
import pytest
from codex_hub.execution_leases import ExecutionStore, ExecutionConflict
from codex_hub.execution_usage import usage_report
from test_execution_leases import lease_task, start, RESULT
from test_agents import token_headers

USAGE={'input_tokens':1200,'cached_input_tokens':800,'output_tokens':70}


def test_completed_and_replayed_result_count_once_and_keep_execution_model(repo,lease_task):
    d=lease_task;db=repo.connection;task_id=d['task']['id']
    db.execute('INSERT INTO hub_task_model_choices VALUES (?,?,?)',(task_id,json.dumps({'model':'gpt-6-astra','reasoning_effort':'medium','source':'user'}),'2026-10-29'))
    db.commit();store=ExecutionStore(repo);run=start(store,d)
    db.execute('UPDATE hub_task_model_choices SET selection_json=? WHERE task_id=?',(json.dumps({'model':'new-preference'}),task_id));db.commit()
    for _ in range(2):store.complete(task_id,d['device']['id'],run['generation'],run['run_key'],**RESULT,token_usage={**USAGE,'private_text':'must not persist'})
    report=usage_report(db,d['project']['id']);row=report['executions'][0]
    assert report['reported_totals']==USAGE and report['measured_execution_count']==1
    assert row['model_id']=='gpt-6-astra' and row['reasoning_effort']=='medium'
    assert row['execution_device_id']==d['device']['id']
    assert 'must not persist' not in str(db.execute('SELECT * FROM hub_execution_usage').fetchall())
    store.complete(task_id,d['device']['id'],run['generation'],run['run_key'],**RESULT,token_usage={**USAGE,'output_tokens':80})
    conflict=usage_report(db,d['project']['id'])
    assert conflict['measured_execution_count']==0 and conflict['executions'][0]['output_tokens']==70
    assert conflict['executions'][0]['conflicting_report']==1


@pytest.mark.parametrize('usage',[None,{},True,{'input_tokens':True},dict(USAGE,output_tokens=-1),dict(USAGE,cached_input_tokens=1300),dict(USAGE,input_tokens=2**53)])
def test_unknown_or_invalid_usage_does_not_block_delivery_or_become_zero(repo,lease_task,usage):
    d=lease_task;store=ExecutionStore(repo);run=start(store,d)
    store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],**RESULT,token_usage=usage)
    result=usage_report(repo.connection,d['project']['id'])
    assert result['measured_execution_count']==0 and result['unknown_execution_count']==1
    assert result['executions'][0]['input_tokens'] is None
    assert repo.get_task(d['task']['id'])['status']=='review'


def test_cancelled_result_cannot_write_usage_and_usage_api_is_scoped(client,repo,lease_task):
    d=lease_task;store=ExecutionStore(repo);run=start(store,d)
    repo.update_task_status(d['task']['id'],'cancelled','cancelled')
    with pytest.raises(ExecutionConflict):
        store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],**RESULT,token_usage=USAGE)
    path=f"/api/projects/{d['project']['slug']}/execution-usage"
    assert client.get(path).status_code in {401,403}
    other=repo.create_device('no access')
    assert client.get(path,headers=token_headers(repo,other)).status_code==403
    result=client.get(path,headers=token_headers(repo,d['device'])).json()
    assert result['measured_execution_count']==0 and run['run_key'] not in json.dumps(result)


def test_actual_http_completion_accepts_usage_and_keeps_fence(client,repo,lease_task):
    d=lease_task;run=start(ExecutionStore(repo),d)
    path=f"/api/projects/{d['project']['slug']}/tasks/{d['task']['id']}/execution/complete"
    payload={**RESULT,'generation':run['generation'],'run_key':run['run_key'],'token_usage':USAGE}
    headers=token_headers(repo,d['device'])
    assert client.post(path,json=payload,headers=headers).status_code==200
    assert client.post(path,json=payload,headers=headers).json()['replayed']
    assert usage_report(repo.connection,d['project']['id'])['reported_totals']==USAGE
