"""Quota refusal recovery uses only synthetic loopback providers."""
import json
from pathlib import Path

import pytest

from codex_bridge.agent_runtime import read_execution_completion
from codex_bridge.client import HubClient, HubClientError
from codex_bridge.runner import BridgeRunner, ProcessResult
from codex_hub.provider_rejections import normalize_rejection
from test_api_model import (SECRET, answer, execute, model_server, setup, tool,
                            loopback_proxy_exclusion)


@pytest.mark.parametrize('status,code,expected', [
    (402, 'Insufficient Balance', 'insufficient_balance'),
    (402, 'invalid_request_error', 'payment_required'),
    (429, 'insufficient_quota', 'insufficient_quota'),
])
def test_first_http_quota_refusal_persists_private_free_proof_and_never_reposts(tmp_path, status, code, expected):
    spec = {'status':status, 'body':{'error':{'code':code, 'message':SECRET}}}
    with model_server([spec]) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        result = execute(config, hub, context)
        proof = {'schema_version':2, 'reason':'quota_exhausted', 'http_status':status,
                 'code':expected, 'request_index':1, 'no_work_observed':True}
        assert not result.ok and result.provider_rejection == proof
        receipt = read_execution_completion(Path(context['receipt_path']), context)
        assert receipt['provider_rejection'] == proof
        assert execute(config, hub, context).provider_rejection == proof
        assert len(received) == 1
        assert not any(call[1].endswith(('/delegate','/revise')) for call in hub.calls)
        for path in tmp_path.glob('completion*'):
            if path.is_file():
                assert SECRET not in path.read_text(encoding='utf-8')


@pytest.mark.parametrize('spec', [
    {'status':429, 'body':{'error':{'code':'rate_limit_exceeded'}}},
    {'status':429, 'body':{'error':{'message':'insufficient_quota'}}},
    {'status':500, 'body':{'error':{'code':'insufficient_quota'}}},
    {'status':402, 'raw':b'{"error":'},
    {'status':402, 'body':{'error':{'code':'insufficient_quota'}, 'choices':[answer()]}},
    {'status':402, 'body':{'error':{'code':'insufficient_quota', 'tool_calls':[]}}},
    {'status':402, 'body':{'error':{'code':'insufficient_quota'}}, 'headers':{'Content-Length':'9999'}},
    {'status':402, 'raw':b'{"error":{"message":"' + b'x' * 66000 + b'"}}'},
])
def test_ambiguous_http_failure_never_gains_retry_proof(tmp_path, spec):
    with model_server([spec]) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        result = execute(config, hub, context)
        assert not result.ok and result.provider_rejection is None
        assert execute(config, hub, context).provider_rejection is None
        assert len(received) == 1


@pytest.mark.parametrize('name,args', [('inspect',{}), ('delegate',{'title':'once','description':'only once'})])
def test_quota_after_prior_assistant_or_mutation_never_allows_replay(tmp_path, name, args):
    replies = [{'body':answer('already started', [tool(name,args)])},
               {'status':402, 'body':{'error':{'code':'insufficient_balance'}}}]
    with model_server(replies) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        result = execute(config, hub, context)
        assert not result.ok and result.provider_rejection is None
        assert execute(config, hub, context).provider_rejection is None
        assert len(received) == 2
        assert len([call for call in hub.calls if call[1].endswith('/delegate')]) == (1 if name == 'delegate' else 0)
        journal = json.loads(Path(context['receipt_path']).with_suffix('.api-journal.json').read_text(encoding='utf-8'))
        assert 'provider_rejection' not in journal


def test_http_rejection_recovery_republishes_receipt_without_another_provider_call(tmp_path):
    with model_server([{'status':402,'body':{'error':{'code':'insufficient_balance'}}}]) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        execution = execute(config, hub, context)
        current = {'generation':context['generation'],'run_key':context['run_key'],'state':'uncertain','task_version':1}
        posted = []
        class Client:
            def reconcile_task_execution(self, *_):
                return {'task':{'id':11,'status':'blocked','status_version':1},'execution':dict(current)}
            def complete_task_execution(self, slug, task_id, fence, **payload):
                posted.append(payload)
                if len(posted) == 1:
                    raise HubClientError('synthetic lost acknowledgement')
                current['state'] = 'yielded'
                return {'accepted':True,'capacity_deferred':True,'task':{'id':11},'execution':dict(current)}
        runner = BridgeRunner(config, client=Client())
        receipt = runner.state.set_task_delivery('agent-test',11,'agent_running')
        receipt.update(run_key=context['run_key'], execution=dict(current), execution_context=context,
                       completion_path=context['receipt_path'],target_project='agent-test')
        runner._save_state()
        runner._recover_agent_executions(ProcessResult())
        assert runner.state.get_task_delivery('agent-test',11)['provider_rejection'] == execution.provider_rejection
        restarted = BridgeRunner(config, client=runner.client)
        restarted._recover_agent_executions(ProcessResult())
        assert len(posted) == 2 and len(received) == 1
        assert all(item['provider_rejection'] == execution.provider_rejection for item in posted)
        assert restarted.state.get_task_delivery('agent-test',11) is None


def test_shared_http_proof_validation_and_client_filter_private_fields():
    proof = {'schema_version':2,'reason':'quota_exhausted','http_status':429,'code':'insufficient_quota',
             'request_index':1,'no_work_observed':True}
    for patch in ({'schema_version':True},{'http_status':200},{'request_index':2},{'request_index':True},
                  {'code':'rate_limit_exceeded'},{'no_work_observed':False},{'code':{}}):
        assert normalize_rejection({**proof,**patch}) is None
    client = HubClient('https://hub.invalid','synthetic-token')
    calls = []
    client._json = lambda *args: calls.append(args) or {}
    client.complete_task_execution('project',1,{'generation':1,'run_key':'key'},status='blocked',summary='quota',
        body='not started',reflection={},provider_rejection={**proof,'raw_response':SECRET})
    assert calls[0][2]['provider_rejection'] == proof and SECRET not in json.dumps(calls)
