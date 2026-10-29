"""Independent lifecycle checks for measured counts and public reply timing."""
import json
import pytest

from codex_hub.execution_leases import ExecutionStore, ExecutionConflict
from codex_hub.execution_usage import usage_report
from codex_hub.execution_telemetry import execution_latency
from test_execution_leases import lease_task, start, RESULT
from test_execution_usage import USAGE


@pytest.mark.parametrize('body',['',' \n\t\u3000','<hub-reflection>{"summary":"internal"}</hub-reflection>',
    '```json\n<hub-reflection>{"summary":"internal"}</hub-reflection>\n```',
    '<hub-artifacts>[]</hub-artifacts>'])
def test_completion_without_visible_body_is_not_a_public_response(repo,lease_task,body):
    d=lease_task;store=ExecutionStore(repo);run=start(store,d)
    store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],**{**RESULT,'body':body})
    row=execution_latency(repo.connection,d['project']['id'])[0]
    assert row['first_public_source'] is None and row['submit_to_first_public_seconds'] is None
    assert 'internal' not in json.dumps(row)


def test_public_text_next_to_internal_reflection_still_counts(repo,lease_task):
    d=lease_task;store=ExecutionStore(repo);run=start(store,d)
    store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],
        **{**RESULT,'body':'实际交付已保存。<hub-reflection>{"summary":"internal"}</hub-reflection>'})
    row=execution_latency(repo.connection,d['project']['id'])[0]
    assert row['first_public_source']=='completed_result'
    assert '实际交付' not in json.dumps(row) and 'internal' not in json.dumps(row)


def test_late_usage_for_preupgrade_execution_keeps_historical_model_unknown(repo,lease_task):
    d=lease_task;db=repo.connection;store=ExecutionStore(repo);run=start(store,d)
    store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],**RESULT)
    db.execute('DELETE FROM hub_execution_usage');db.execute('INSERT OR REPLACE INTO hub_task_model_choices VALUES (?,?,?)',
        (d['task']['id'],json.dumps({'model':'new-model-now','reasoning_effort':'high','source':'user'}),'now'));db.commit()
    store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],**RESULT,token_usage=USAGE)
    row=usage_report(db,d['project']['id'])['executions'][0]
    assert row['input_tokens']==USAGE['input_tokens'] and not row['model_snapshot_known'] and row['model_id']==''


def test_changed_result_or_wrong_run_cannot_change_previously_measured_counts(repo,lease_task):
    d=lease_task;store=ExecutionStore(repo);run=start(store,d)
    store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],**RESULT,token_usage=USAGE)
    for key,body in [('wrong-run-key',RESULT['body']),(run['run_key'],'changed result')]:
        with pytest.raises(ExecutionConflict):
            store.complete(d['task']['id'],d['device']['id'],run['generation'],key,
                **{**RESULT,'body':body},token_usage={**USAGE,'output_tokens':999})
    report=usage_report(repo.connection,d['project']['id'])
    assert report['reported_totals']==USAGE and report['executions'][0]['conflicting_report']==0


def test_new_execution_device_does_not_reassign_old_usage_or_timings(repo,lease_task):
    d=lease_task;db=repo.connection;store=ExecutionStore(repo);run=start(store,d)
    store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],**RESULT,token_usage=USAGE)
    other=repo.create_device('actual second executor');repo.add_project_member(d['project']['id'],other['id'],'write')
    repo.update_task_status(d['task']['id'],'open','new generation')
    db.execute('UPDATE tasks SET assigned_device_id=? WHERE id=?',(other['id'],d['task']['id']));db.commit()
    new=start(store,{**d,'device':other},'new-device-generation')
    zero={key:0 for key in USAGE}
    store.complete(d['task']['id'],other['id'],new['generation'],new['run_key'],**RESULT,token_usage=zero)
    report=usage_report(db,d['project']['id'])
    assert report['reported_totals']==USAGE and report['measured_execution_count']==2
    assert [row['execution_device_id'] for row in report['executions']]==[other['id'],d['device']['id']]
    assert [row['execution_device_id'] for row in execution_latency(db,d['project']['id'])]==[other['id'],d['device']['id']]
