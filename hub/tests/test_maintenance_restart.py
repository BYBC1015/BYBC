"""A source worker upgrade must retain uncertain work without replaying it."""
import json
from pathlib import Path
import time
import uuid

from codex_bridge.project_sessions import ProjectSessionStore
from codex_bridge.runner import BridgeRunner
from codex_bridge.state import BridgeState
from codex_bridge.task_pool import DurableTaskPool, task_config
from codex_hub.execution_leases import ExecutionStore
from test_agent_runtime import config_for
from test_agents import token_headers
from test_execution_leases import lease_task
from test_task_pool import ApiClient


def test_pool_restart_retains_blocked_uncertain_project_without_completion_or_cli(client, repo, lease_task, tmp_path):
    """Reproduce the observed task #40 state using only synthetic API fixtures."""
    data = lease_task
    slug = data['project']['slug']
    task_id = data['task']['id']
    repo.connection.execute("UPDATE tasks SET execution_route='project' WHERE id=?", (task_id,))
    repo.connection.commit()
    clock = [1000.0]
    executions = ExecutionStore(repo, clock=lambda:clock[0])
    acquired = executions.acquire(task_id, data['device']['id'], uuid.uuid4().hex)['execution']
    execution = executions.start(task_id, data['device']['id'], acquired['generation'], acquired['run_key'])['execution']
    clock[0] += 200
    observed = executions.reconcile(task_id, data['device']['id'])
    assert observed['task']['status'] == 'blocked'
    assert observed['execution']['state'] == 'uncertain'
    task = {**repo.get_task(task_id), 'assigned_agent_kind':'worker'}
    base = config_for(tmp_path)
    project_path = tmp_path/'project'
    project_path.mkdir(exist_ok=True)
    base.projects = {slug:project_path}
    runtime = task_config(base, task)
    session_root = Path(runtime.backend_options['project_session_root'])
    with ProjectSessionStore(session_root).acquire(device_id=data['device']['id'], project_slug=slug,
            conversation_id=task['conversation_id'], assigned_agent_id=task['assigned_agent_id'],
            project_path=project_path, run_key=execution['run_key'], generation=execution['generation']) as session:
        session.start()
        session.observe_jsonl(json.dumps({'type':'thread.started','thread_id':str(uuid.uuid4())}))
        project_receipt = session.receipt_path
    assert not project_receipt.exists()
    completion = tmp_path/'missing-native-completion.json'
    state = BridgeState.load(runtime.state_path)
    receipt = state.set_task_delivery(slug, task_id, 'agent_running')
    receipt.update(run_key=execution['run_key'], target_project=slug, execution=execution,
                   completion_path=str(completion), execution_context={**execution,'task_id':task_id,
                   'project_slug':slug,'receipt_path':str(completion),'execution_route':'project',
                   'conversation_id':task['conversation_id'],'assigned_agent_id':task['assigned_agent_id'],
                   'assigned_agent_kind':'worker','project_path':str(project_path)})
    state.save(runtime.state_path)
    meta = runtime.state_path.parent/'task.json'
    meta.write_text(json.dumps(task), encoding='utf-8')
    headers = token_headers(repo, data['device'])
    calls = []

    class NeverExecute:
        def run(self, *_args, **_kwargs):
            calls.append('forbidden replay')
            raise AssertionError('Uncertain work must never launch CLI after maintenance')

    def factory(item):
        api = ApiClient(client, headers, item['id'])
        runner = BridgeRunner(task_config(base, item), client=api, executor=NeverExecute())
        runner._next_capability_report = float('inf')
        api.pending_task_ids = lambda:{task_id}
        return runner

    for _ in range(2):
        pool = DurableTaskPool(base, ApiClient(client, headers), runner_factory=factory)
        assert task_id in pool.metadata  # task.json keeps recovery discoverable, despite hidden inbox
        try:
            deadline = time.monotonic()+6
            while task_id not in pool.retry_after and time.monotonic()<deadline:
                pool.tick()
                time.sleep(.01)
            assert task_id in pool.retry_after
            assert not calls
            retained = BridgeState.load(runtime.state_path).get_task_delivery(slug, task_id)
            assert retained['stage'] == 'agent_running'
            assert retained['run_key'] == execution['run_key']
            assert retained['execution']['state'] == 'uncertain'
            assert not completion.exists() and not project_receipt.exists()
            assert repo.get_task(task_id)['status'] == 'blocked'
            assert repo.connection.execute('SELECT COUNT(*) FROM hub_task_executions WHERE task_id=?', (task_id,)).fetchone()[0] == 1
        finally:
            pool.close()
