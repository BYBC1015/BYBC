"""Usage is measured from CLI completion events, never inferred from text."""
import json
from pathlib import Path
import subprocess

import pytest

from codex_bridge.client import HubClient
from codex_bridge.runner import BridgeRunner
from codex_bridge.token_usage import normalize_token_usage, usage_from_jsonl
from test_controller_chat_sessions import chat
from test_project_exec import SessionProcess, configuration, execute


USAGE = {'input_tokens': 1040, 'cached_input_tokens': 800, 'output_tokens': 42}


def event(usage=USAGE, **fields):
    return json.dumps({'type': 'turn.completed', 'usage': usage, **fields})


def test_only_real_complete_usage_allowlist_is_collected():
    output = '\n'.join([json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': event()}}),
        json.dumps({'type': 'reasoning', 'usage': USAGE}), event({**USAGE, 'reasoning': 'private'})])
    assert usage_from_jsonl(output) == USAGE
    assert usage_from_jsonl('') is None
    assert usage_from_jsonl(json.dumps({'type': 'turn.failed', 'usage': USAGE})) is None
    assert usage_from_jsonl(event({'input_tokens': 0, 'cached_input_tokens': 0, 'output_tokens': 0})) == dict.fromkeys(USAGE, 0)


@pytest.mark.parametrize('bad', [None, {}, {'input_tokens': 2, 'output_tokens': 1},
    {**USAGE, 'input_tokens': True}, {**USAGE, 'output_tokens': -1},
    {**USAGE, 'cached_input_tokens': 1041}, {**USAGE, 'input_tokens': 1 << 53}])
def test_unknown_or_invalid_usage_is_absent_not_estimated(bad):
    assert normalize_token_usage(bad) is None
    assert usage_from_jsonl(event(bad)) is None


def test_replayed_usage_is_not_counted_twice_and_ambiguous_events_stay_unknown():
    same = event(turn_id='known-one')
    assert usage_from_jsonl(same + '\n' + same) == USAGE
    second = event({'input_tokens': 100, 'cached_input_tokens': 0, 'output_tokens': 10}, turn_id='known-two')
    assert usage_from_jsonl(same + '\n' + second) == {'input_tokens': 1140, 'cached_input_tokens': 800, 'output_tokens': 52}
    assert usage_from_jsonl(event() + '\n' + event()) is None
    assert usage_from_jsonl(same + '\n' + event({**USAGE, 'output_tokens': 60}, turn_id='known-one')) is None


class UsageProcess(SessionProcess):
    def run(self, *args, **kwargs):
        result = super().run(*args, **kwargs)
        result.stdout = result.stdout.replace(json.dumps({'type': 'turn.completed'}), event())
        return result


def test_real_session_and_outer_receipts_preserve_usage_without_new_turn(tmp_path):
    config = configuration(tmp_path)
    process = UsageProcess()
    ctx = chat(tmp_path)
    first = execute(config, process, ctx)
    assert first.ok and first.token_usage == USAGE
    receipt = json.loads(Path(ctx['receipt_path']).read_text(encoding='utf-8'))
    assert receipt['token_usage'] == USAGE and receipt['run_key'] == ctx['run_key']
    replay = execute(config, process, ctx)
    assert replay.token_usage == USAGE and len(process.calls) == 1


def test_controller_session_completion_recovers_crash_before_outer_receipt_without_reexecution(tmp_path):
    config = configuration(tmp_path)
    process = UsageProcess()
    ctx = chat(tmp_path)
    assert execute(config, process, ctx).ok
    Path(ctx['receipt_path']).unlink()  # Simulate the exact crash window in isolated temp state.
    runner = object.__new__(BridgeRunner)
    runner.config = config
    receipt = {'completion_path': ctx['receipt_path'], 'execution_context': ctx,
        'task_id': ctx['task_id'], 'project_slug': 'project-one', 'target_project': 'project-one'}
    recovered = runner._read_agent_completion(receipt, ctx)
    assert recovered['ok'] and recovered['token_usage'] == USAGE
    assert len(process.calls) == 1
    other_room = {**receipt, 'execution_context': {**ctx, 'conversation_id': 13}}
    assert runner._read_agent_completion(other_room, ctx) is None
    read_only = {**receipt, 'execution_context': {**ctx, 'controller_read_only': True}}
    assert runner._read_agent_completion(read_only, ctx) is None


def test_completion_api_sends_measured_counts_and_omits_unknown_for_old_receipts():
    captured = []
    client = HubClient('https://hub.example', 'never-printed')
    client._json = lambda method, path, body: captured.append(body) or {}
    for usage in (USAGE, None):
        client.complete_task_execution('agent-test', 1, {'generation': 2, 'run_key': 'same-idempotent-run'},
            status='review', summary='done', body='done', reflection={}, token_usage=usage)
    assert captured[0]['token_usage'] == USAGE
    assert 'token_usage' not in captured[1]
    assert captured[0]['generation'] == 2 and captured[0]['run_key'] == 'same-idempotent-run'


def test_timeout_after_actual_project_activity_preserves_cause_and_does_not_replay(tmp_path):
    config = configuration(tmp_path)
    class TimedOutProcess(SessionProcess):
        def run(self, command, **kwargs):
            self.calls.append(command)
            output = json.dumps({'type': 'thread.started', 'thread_id': self.session})
            raise subprocess.TimeoutExpired(command, kwargs['timeout'], output=output, stderr='')
    process = TimedOutProcess()
    result = execute(config, process, chat(tmp_path))
    assert not result.ok and result.returncode is None
    assert 'timed out after 1800 seconds' in result.error
    assert '项目执行结果不明确' in result.error
    receipt = json.loads(Path(chat(tmp_path)['receipt_path']).read_text(encoding='utf-8'))
    assert 'timed out after 1800 seconds' in receipt['error']
    assert 'token_usage' not in receipt
    assert not execute(config, process, chat(tmp_path, 'do-not-repeat')).ok
    assert len(process.calls) == 1
