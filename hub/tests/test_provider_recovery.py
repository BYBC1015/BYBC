"""Synthetic provider refusals only; no paid requests or real task mutations."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import uuid

import pytest

from codex_bridge.codex_exec import CodexExecResult
from codex_bridge.provider_health import before_work_rejection, normalize_rejection
from test_project_exec import configuration, context, execute, SessionProcess
from codex_bridge.runner import BridgeRunner, ProcessResult
from codex_bridge.client import HubClient, HubClientError


DENIAL = {'type': 'turn.failed', 'error': {'code': 'usage_limit_reached',
    'message': "You've hit your usage limit. Please try again later."}}


def rejected(*events, **changes):
    result = CodexExecResult(ok=False, command=['codex', '--model', 'gpt-6-sol', 'exec', '--json', '-'],
        returncode=1, stdout='\n'.join(json.dumps(event) for event in events), stderr='',
        last_message='', output_path=Path('unused.md'), protocol_complete=True)
    return replace(result, **changes)


def test_provider_refusal_before_any_work_has_bounded_non_sensitive_evidence():
    refusal = dict(DENIAL, error={'message': "You've hit your usage limit. https://example.invalid/?token=private"})
    result = rejected({'type':'thread.started','thread_id':str(uuid.uuid4())},
        {'type':'turn.started'}, {'type':'error','message':"You've hit your usage limit."}, refusal)
    proof = before_work_rejection(result)
    assert proof['reason'] == 'quota_exhausted' and proof['no_work_observed'] is True
    assert proof['event_types'] == ['thread.started','turn.started','error','turn.failed']
    assert len(proof['stream_sha256']) == 64 and normalize_rejection(proof) == proof
    assert 'private' not in json.dumps(proof)
    assert before_work_rejection(rejected(DENIAL)) is not None


@pytest.mark.parametrize('item_type', ['agent_message','reasoning','command_execution','mcp_tool_call',
                                     'file_change','web_search','todo_list'])
def test_any_model_or_tool_item_prevents_automatic_replay(item_type):
    item = {'type':'item.completed','item':{'type':item_type,'text':'already emitted'}}
    assert before_work_rejection(rejected(item, DENIAL)) is None


@pytest.mark.parametrize('changes', [
    {'ok':True}, {'returncode':None}, {'returncode':-1}, {'returncode':True},
    {'protocol_complete':False}, {'last_message':'partial reply'},
    {'command':['codex','exec']}, {'stdout':'not json\n' + json.dumps(DENIAL)},
    {'stdout':json.dumps(DENIAL) + '\n{"type":'},
    {'stdout':' ' * 65537 + json.dumps(DENIAL)},
    {'stdout':'\ud800' + json.dumps(DENIAL)},
    {'stdout':json.dumps({'type':'turn.failed','error':{'code':'connection_error','message':'429 while retrying'}})},
    {'stdout':json.dumps({'type':'turn.failed','error':{'message':'Tool says usage limit'}})},
    {'stdout':json.dumps({'type':'turn.failed','error':{'code':'usage_limit_reached','hidden_output':'unknown'}})},
])
def test_ambiguous_failed_and_partial_invocations_do_not_gain_safe_retry(changes):
    assert before_work_rejection(rejected(DENIAL, **changes)) is None


def test_unknown_events_or_a_successful_turn_before_refusal_prevent_replay():
    for event in [{'type':'turn.completed'}, {'type':'new.future.event'}, {'type':'error','message':'stream disconnected'},
                  {'type':'turn.started','payload':'unexpected output'}, DENIAL]:
        assert before_work_rejection(rejected(event, DENIAL)) is None
    for event in [{'type':{}}, {'type':[]}, {'type':None}]:
        assert before_work_rejection(rejected(event, DENIAL)) is None
    proof = before_work_rejection(rejected(DENIAL))
    for changed in [{'schema_version':True}, {'code':{}}, {'event_types':[{}]}]:
        assert normalize_rejection({**proof, **changed}) is None


def test_out_of_order_rejection_events_cannot_authorize_replay():
    events = [{'type':'error', 'message':"You've hit your usage limit"}, {'type':'turn.started'}, DENIAL]
    assert before_work_rejection(rejected(*events)) is None
    proof = before_work_rejection(rejected(DENIAL))
    assert normalize_rejection({**proof, 'event_types':[item['type'] for item in events]}) is None


class QuotaProcess(SessionProcess):
    def __init__(self, *, has_thread=False, partial=False):
        super().__init__()
        self.has_thread, self.partial = has_thread, partial

    def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        events = []
        if self.has_thread:
            events.append({'type':'thread.started','thread_id':self.session})
        events.append({'type':'turn.started'})
        if self.partial:
            events.append({'type':'item.completed','item':{'type':'command_execution','exit_code':0}})
        events.append(DENIAL)
        return subprocess.CompletedProcess(command, 1, '\n'.join(json.dumps(event) for event in events), '')


@pytest.mark.parametrize('has_thread', [False, True])
def test_initial_quota_denial_closes_only_this_run_and_replays_its_receipt(tmp_path, has_thread):
    config = configuration(tmp_path)
    process = QuotaProcess(has_thread=has_thread)
    ctx = context(tmp_path)
    first = execute(config, process, ctx)
    assert not first.ok and first.provider_rejection
    native = json.loads(Path(ctx['receipt_path']).read_text(encoding='utf-8'))
    assert native['provider_rejection'] == first.provider_rejection
    binding = json.loads(next((tmp_path/'project-sessions'/'bindings').glob('*.json')).read_text())
    assert binding['pending_run'] is None
    assert binding['session_id'] == (process.session if has_thread else None)
    repeated = execute(config, process, ctx)
    assert repeated.provider_rejection == first.provider_rejection and len(process.calls) == 1
    # A separately authorized task/run may proceed; never rerun the rejected key.
    next_process = SessionProcess()
    if has_thread:
        next_process.session = process.session
    newer = context(tmp_path, 'server-authorized-followup')
    assert execute(config, next_process, newer).ok
    assert ('resume' in next_process.calls[0][0]) is has_thread


def test_uncertain_project_without_thread_keeps_lock_and_does_not_retry(tmp_path):
    config = configuration(tmp_path)
    process = QuotaProcess(partial=True)
    ctx = context(tmp_path)
    first = execute(config, process, ctx)
    assert not first.ok and not first.provider_rejection
    binding = json.loads(next((tmp_path/'project-sessions'/'bindings').glob('*.json')).read_text())
    assert binding['pending_run'] is not None
    assert not execute(config, process, context(tmp_path, 'unsafe-repeat')).ok
    assert len(process.calls) == 1


@pytest.mark.parametrize('ack_lost_after_commit', [False, True])
def test_restart_republishes_only_refusal_receipt_and_never_runs_cli_again(tmp_path, ack_lost_after_commit):
    config = configuration(tmp_path)
    process = QuotaProcess()
    ctx = context(tmp_path)
    execution_result = execute(config, process, ctx)
    current = {'generation':ctx['generation'], 'run_key':ctx['run_key'], 'state':'uncertain', 'task_version':1}
    posted = []

    class Client:
        def reconcile_task_execution(self, *args):
            return {'task':{'id':1,'status':'blocked','status_version':1}, 'execution':dict(current)}

        def complete_task_execution(self, slug, task_id, execution, **payload):
            posted.append(payload)
            if len(posted) == 1:
                if ack_lost_after_commit:
                    current['state'] = 'yielded'
                raise HubClientError('unknown acknowledgement')
            current['state'] = 'yielded'
            return {'accepted':True, 'task':{'id':1}, 'execution':dict(current)}

    client = Client()
    runner = BridgeRunner(config, client=client)
    receipt = runner.state.set_task_delivery('project-one', 1, 'agent_running')
    receipt.update(run_key=ctx['run_key'], execution=dict(current), execution_context=ctx,
                   completion_path=ctx['receipt_path'], target_project='project-one')
    runner._save_state()
    runner._recover_agent_executions(ProcessResult())
    assert runner.state.get_task_delivery('project-one',1)['stage'] == 'agent_result'
    restarted = BridgeRunner(config, client=client)
    restarted._recover_agent_executions(ProcessResult())
    assert len(posted) == (1 if ack_lost_after_commit else 2)
    assert all(item['provider_rejection'] == execution_result.provider_rejection for item in posted)
    assert all(item['status'] == 'blocked' for item in posted)
    assert len(process.calls) == 1
    assert restarted.state.get_task_delivery('project-one',1) is None
    assert '1' not in restarted.state.seen_task_ids  # server explicitly yielded, not a local retry


def test_client_sends_only_normalized_refusal_and_old_receipts_have_none():
    client = HubClient('https://hub.example','fixture')
    calls = []
    client._json = lambda *args: calls.append(args) or {}
    proof = before_work_rejection(rejected(DENIAL))
    payload = dict(status='blocked',summary='quota unavailable',body='not started',reflection={})
    execution = {'generation':1,'run_key':'stable-rejection-key'}
    client.complete_task_execution('project-one',1,execution,**payload,
                                   provider_rejection={**proof,'raw_provider_text':'never-upload'})
    assert calls[-1][2]['provider_rejection'] == proof
    assert 'never-upload' not in json.dumps(calls)
    client.complete_task_execution('project-one',1,execution,**payload)
    assert 'provider_rejection' not in calls[-1][2]
    with pytest.raises(HubClientError):
        client.complete_task_execution('project-one',1,execution,**payload,provider_rejection={'reason':'quota'})
