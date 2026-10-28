import json
import time
import os
import sys
import threading
from pathlib import Path

import pytest

from codex_bridge.client import HubClientError
from codex_bridge.codex_exec import CodexExecRunner
from codex_bridge.agent_runtime import ExecutionRenewal, read_execution_completion
from codex_bridge.runner import BridgeRunner
from codex_hub.agents import AgentStore
from codex_hub.execution_leases import ExecutionStore, ExecutionConflict
from test_agent_runtime import AgentClient, Decider, config_for, REFLECTION
from test_codex_bridge import FakeExecutor, FakeLauncher
from test_codex_exec import FakeProcessRunner


@pytest.fixture(autouse=True)
def isolated_model_allocator(monkeypatch):
    # Model planning is exercised separately; fault recovery never calls a paid model.
    monkeypatch.setattr('codex_bridge.model_allocator.allocate_task_model', lambda *args: {})


class StoreClient(AgentClient):
    def __init__(self, repo):
        super().__init__(existing=True)
        self.repo = repo
        self.device = repo.create_device(label='isolated execution')
        self.project = repo.create_project(slug='agent-test', name='Recovery', created_by_device_id=self.device['id'])
        agents = AgentStore(repo.connection)
        agent = agents.create(self.project['id'], 'Executor', 'Test', device_id=self.device['id'])
        task = repo.create_task(self.project['id'], 'Recover actual result', assigned_agent_id=agent['id'])
        agents.route(task['id'], {'action':'new','title':'Recovery','reason':'isolated test'}, 'codex_cli')
        self.task_id = task['id']
        self.store = ExecutionStore(repo)
        self.hide_inbox = False
        self.start_calls = self.complete_calls = 0
        self.fail_start_ack = self.fail_complete_ack = self.fail_complete_before = False
        self.inbox = {'device':{'id':self.device['id'],'label':'isolated'},'tasks':[],'mentions':[]}

    def get_inbox(self):
        task = self.repo.get_task(self.task_id)
        self.inbox['tasks'] = [] if self.hide_inbox else [{**task,'project_slug':'agent-test'}]
        return self.inbox

    def update_task_status(self, slug, task_id, status, result_summary='', reflection=None):
        return self.repo.update_task_status(task_id, status, result_summary, reflection=reflection,
                                           actor_device_id=self.device['id'])

    def acquire_task_execution(self, slug, task_id, run_key):
        return self.store.acquire(task_id, self.device['id'], run_key)

    def start_task_execution(self, slug, task_id, execution):
        self.start_calls += 1
        value = self.store.start(task_id, self.device['id'], execution['generation'], execution['run_key'])
        if self.fail_start_ack:
            raise HubClientError('lost start acknowledgement')
        return value

    def reconcile_task_execution(self, slug, task_id):
        return self.store.reconcile(task_id, self.device['id'])

    def renew_task_execution(self, slug, task_id, execution):
        return self.store.renew(task_id, self.device['id'], execution['generation'], execution['run_key'])

    def complete_task_execution(self, slug, task_id, execution, **payload):
        self.complete_calls += 1
        if self.fail_complete_before:
            raise HubClientError('network unavailable')
        try:
            value = self.store.complete(task_id, self.device['id'], execution['generation'], execution['run_key'], **payload)
        except ExecutionConflict as exc:
            raise HubClientError(str(exc), status_code=409) from None
        if self.fail_complete_ack:
            raise HubClientError('lost complete acknowledgement')
        return value


class ReceiptExecutor(FakeExecutor):
    def run(self, *args, execution_context=None, cancel_event=None, **kwargs):
        result = super().run(*args, **kwargs)
        path = Path(execution_context['receipt_path'])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**{k:execution_context[k] for k in ('generation','run_key')},
            'ok':result.ok,'returncode':result.returncode,'last_message':result.last_message,
            'error':result.error,'output_path':str(result.output_path)}), encoding='utf-8')
        return result


def runtime(repo, tmp_path):
    client = StoreClient(repo)
    executor = ReceiptExecutor(summary='Completed\n<hub-reflection>' + json.dumps(REFLECTION) + '</hub-reflection>')
    runner = BridgeRunner(config_for(tmp_path), client=client, executor=executor,
                         launcher=FakeLauncher(), agent_decider=Decider())
    return runner, client, executor


def restart(runner, client, executor):
    return BridgeRunner(runner.config, client=client, executor=executor,
                       launcher=FakeLauncher(), agent_decider=Decider())


def test_unknown_complete_ack_recovers_without_inbox_or_duplicate_work(repo, tmp_path):
    runner, client, executor = runtime(repo, tmp_path)
    client.fail_complete_ack = True
    runner.process_once()
    assert repo.get_task(client.task_id)['status'] == 'review'
    assert runner.state.get_task_delivery('agent-test', client.task_id)['stage'] == 'agent_result'
    client.hide_inbox = True
    recovered = restart(runner, client, executor)
    recovered.process_once()
    assert len(executor.calls) == client.start_calls == client.complete_calls == 1
    assert repo.connection.execute("SELECT COUNT(*) FROM entries WHERE task_id=? AND kind='markdown'", (client.task_id,)).fetchone()[0] == 1
    assert repo.connection.execute("SELECT COUNT(*) FROM entries WHERE task_id=? AND metadata_json LIKE '%completion_notice%'", (client.task_id,)).fetchone()[0] == 1
    assert recovered.state.get_task_delivery('agent-test', client.task_id) is None


def test_worker_crash_after_cli_completion_uses_native_receipt_without_inbox(repo, tmp_path, monkeypatch):
    runner, client, executor = runtime(repo, tmp_path)
    def crash(*args):
        raise RuntimeError('simulated worker interruption after process exit')
    monkeypatch.setattr(runner, '_save_agent_result', crash)
    with pytest.raises(RuntimeError):
        runner.process_once()
    assert repo.get_task(client.task_id)['status'] == 'in_progress'
    client.hide_inbox = True
    recovered = restart(runner, client, executor)
    recovered.process_once()
    assert repo.get_task(client.task_id)['status'] == 'review'
    assert len(executor.calls) == client.start_calls == 1
    assert repo.connection.execute("SELECT COUNT(*) FROM entries WHERE task_id=? AND kind='markdown'", (client.task_id,)).fetchone()[0] == 1
    assert repo.connection.execute("SELECT COUNT(*) FROM entries WHERE task_id=? AND metadata_json LIKE '%completion_notice%'", (client.task_id,)).fetchone()[0] == 1


@pytest.mark.parametrize('drop_mapping',[False,True])
def test_crash_between_project_receipt_and_worker_completion_recovers_without_cli(repo,tmp_path,monkeypatch,drop_mapping):
    from codex_bridge.project_sessions import ProjectSessionStore
    import uuid
    runner,client,_=runtime(repo,tmp_path)
    task=repo.get_task(client.task_id)
    client.context={'agent':{'id':task['assigned_agent_id']},'task':{
        **task,'execution_route':'project','assigned_device_id':client.device['id']}}
    calls=[]
    class CrashAfterSession:
        def run(self,slug,workspace,prompt_path,prompt,*,execution_context=None,**kwargs):
            calls.append(1);ctx=execution_context
            with ProjectSessionStore(runner.config.state_path.parent/'project-sessions').acquire(
                device_id=ctx['device_id'],project_slug=slug,conversation_id=ctx['conversation_id'],
                assigned_agent_id=ctx['assigned_agent_id'],project_path=workspace,
                run_key=ctx['run_key'],generation=ctx['generation']) as session:
                session.start()
                session.observe_jsonl(json.dumps({'type':'thread.started','thread_id':str(uuid.uuid4())})+'\n'+json.dumps({'type':'turn.completed'}))
                session.complete(ok=True,returncode=0,last_message='已完成\n<hub-reflection>'+json.dumps(REFLECTION)+'</hub-reflection>',output_path=prompt_path)
            raise RuntimeError('crash before worker completion')
    executor=CrashAfterSession();runner.executor=executor
    with pytest.raises(RuntimeError,match='crash before worker'):
        runner.process_once()
    receipt=runner.state.get_task_delivery('agent-test',client.task_id)
    assert not Path(receipt['completion_path']).exists()
    if drop_mapping:
        runner.config.projects.clear()
    client.hide_inbox=True
    recovered=restart(runner,client,executor)
    recovered.process_once()
    assert repo.get_task(client.task_id)['status']=='review'
    assert calls==[1] and client.start_calls==1
    assert repo.connection.execute("SELECT COUNT(*) FROM entries WHERE task_id=? AND kind='markdown'",(client.task_id,)).fetchone()[0]==1
    assert repo.connection.execute("SELECT COUNT(*) FROM entries WHERE task_id=? AND metadata_json LIKE '%completion_notice%'",(client.task_id,)).fetchone()[0]==1


def test_cancelled_result_stays_local_and_never_overwrites_human_state(repo, tmp_path):
    runner, client, executor = runtime(repo, tmp_path)
    client.fail_complete_before = True
    runner.process_once()
    repo.update_task_status(client.task_id, 'cancelled', 'Human cancellation')
    client.fail_complete_before, client.hide_inbox = False, True
    recovered = restart(runner, client, executor)
    recovered.process_once()
    assert repo.get_task(client.task_id)['status'] == 'cancelled'
    assert repo.connection.execute('SELECT COUNT(*) FROM entries WHERE task_id=?', (client.task_id,)).fetchone()[0] == 0
    assert recovered.state.get_task_delivery('agent-test', client.task_id)['stage'] == 'agent_result_rejected'
    assert len(executor.calls) == 1


def test_unknown_start_ack_never_starts_model_or_repeats_start(repo, tmp_path):
    runner, client, executor = runtime(repo, tmp_path)
    client.fail_start_ack = True
    runner.process_once()
    recovered = restart(runner, client, executor)
    recovered.process_once()
    assert executor.calls == [] and client.start_calls == 1
    receipt = recovered.state.get_task_delivery('agent-test', client.task_id)
    # Merely seeing a last-message file cannot turn unknown execution into success.
    Path(receipt['completion_path']).parent.mkdir(parents=True, exist_ok=True)
    Path(receipt['completion_path']).write_text('final answer', encoding='utf-8')
    recovered.process_once()
    assert client.complete_calls == 0 and executor.calls == []
    repo.connection.execute('UPDATE hub_task_executions SET lease_until=0')
    repo.connection.commit()
    recovered.process_once()
    assert repo.get_task(client.task_id)['status'] == 'blocked'
    assert executor.calls == [] and client.start_calls == 1


def test_prepared_receipt_can_resume_after_lost_acquire_ack(repo, tmp_path):
    runner, client, executor = runtime(repo, tmp_path)
    original = client.acquire_task_execution
    def lost(*args):
        original(*args)
        raise HubClientError('lost acquire acknowledgement')
    client.acquire_task_execution = lost
    runner.process_once()
    assert executor.calls == []
    client.acquire_task_execution = original
    recovered = restart(runner, client, executor)
    recovered.process_once()
    assert len(executor.calls) == client.start_calls == 1
    assert repo.get_task(client.task_id)['status'] == 'review'


def test_expired_prepared_receipt_obtains_new_generation_before_first_work(repo, tmp_path):
    runner, client, executor = runtime(repo, tmp_path)
    original = client.start_task_execution
    client.start_task_execution = lambda *args: (_ for _ in ()).throw(HubClientError('never reached server'))
    runner.process_once()
    repo.connection.execute('UPDATE hub_task_executions SET lease_until=0')
    repo.connection.commit()
    client.start_task_execution = original
    recovered = restart(runner, client, executor)
    recovered.process_once()
    current = client.reconcile_task_execution('agent-test', client.task_id)['execution']
    assert current['generation'] == 2 and current['state'] == 'completed'
    assert len(executor.calls) == 1


def test_actual_cli_wrapper_persists_exit_receipt_and_scoped_helper_environment(tmp_path):
    config = config_for(tmp_path)
    config.codex_cli_path = tmp_path/'fake-cli.exe'
    config.codex_cli_path.write_text('fake', encoding='utf-8')
    prompt = tmp_path/'prompt.md'
    prompt.write_text('task', encoding='utf-8')
    context = {'generation':3,'run_key':'stable-runtime-key-123','task_id':7,'project_slug':'agent-test',
               'receipt_path':str(tmp_path/'completion.json')}
    process = FakeProcessRunner(returncode=1)
    outcome = CodexExecRunner(config, process).run('agent-test', config.projects['agent-test'], prompt,
        'read task', execution_preferences={'model':'gpt-6-sol','reasoning_effort':'high'}, execution_context=context)
    receipt = read_execution_completion(Path(context['receipt_path']), context)
    assert not outcome.ok and receipt['returncode'] == 1 and receipt['ok'] is False
    command, options = process.calls[0]
    assert options['env']['CODEX_HUB_EXECUTION_TASK_ID'] == '7'
    assert options['env']['CODEX_HUB_EXECUTION_RUN_KEY'] == context['run_key']
    assert context['run_key'] not in options['input']
    assert command[command.index('--model') + 1] == 'gpt-6-sol'
    assert read_execution_completion(Path(context['receipt_path']), {**context,'generation':4}) is None


def test_renewal_network_failure_never_starts_replacement():
    class Client:
        timeout_seconds = 1
        calls = 0
        def renew_task_execution(self, *args):
            self.calls += 1
            raise HubClientError('network unavailable')
    client = Client()
    with ExecutionRenewal(client, 'agent-test', 7, {'generation':1,'run_key':'stable-runtime-key-123'}, interval=.01) as renewal:
        deadline = time.monotonic() + 2
        while client.calls < 2 and time.monotonic() < deadline:
            time.sleep(.01)
    assert client.calls >= 2 and not renewal.thread.is_alive() and not renewal.rejected


def test_model_allocation_failure_is_reported_once_without_work(repo, tmp_path, monkeypatch):
    runner, client, executor = runtime(repo, tmp_path)
    calls = []
    def fail(*args):
        calls.append(args)
        raise ValueError('no supported model choice')
    monkeypatch.setattr('codex_bridge.model_allocator.allocate_task_model', fail)
    runner.process_once()
    runner.process_once()
    assert len(calls) == 1 and executor.calls == []
    assert repo.get_task(client.task_id)['status'] == 'blocked'
    assert runner.state.get_task_delivery('agent-test', client.task_id)['status_reported']


def test_frozen_model_selection_reaches_actual_execution(repo, tmp_path):
    runner, client, executor = runtime(repo, tmp_path)
    original = client.acquire_task_execution
    choice = {'model':'gpt-5.6-sol','reasoning_effort':'high','source':'controller_task_plan','reason':'bounded work'}
    def acquire(*args):
        return {**original(*args), 'model_selection':choice}
    client.acquire_task_execution = acquire
    runner.process_once()
    assert executor.calls[0][4] == choice


def test_missing_execution_api_does_not_fall_back_to_legacy_work(repo, tmp_path):
    runner, client, executor = runtime(repo, tmp_path)
    def unavailable(*args):
        raise HubClientError('protocol endpoint unavailable', status_code=404)
    client.acquire_task_execution = unavailable
    runner.process_once()
    runner.process_once()
    assert executor.calls == [] and client.start_calls == 0
    assert repo.get_task(client.task_id)['status'] == 'open'


def test_explicit_lease_rejection_sets_cancel_signal_but_network_errors_do_not():
    class Client:
        timeout_seconds = 1
        def renew_task_execution(self, *args):
            raise HubClientError('cancelled', status_code=409)
    with ExecutionRenewal(Client(), 'agent-test', 1, {}, interval=.01) as renewal:
        assert renewal.cancelled.wait(2)
    assert renewal.rejected


def test_owned_process_tree_stops_on_explicit_cancel_and_drains_output(tmp_path):
    marker = tmp_path / 'child.pid'
    child = f"import os,time,pathlib;pathlib.Path({str(marker)!r}).write_text(str(os.getpid()));time.sleep(30)"
    parent = ('import subprocess,sys,time;'
              f'child=subprocess.Popen([sys.executable,"-c",{child!r}]);'
              'sys.stdout.write("x"*200000);sys.stdout.flush();time.sleep(30)')
    event = threading.Event()
    def cancel():
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        event.set()
    trigger = threading.Thread(target=cancel)
    trigger.start()
    runner = CodexExecRunner(config_for(tmp_path))
    result = runner._run_cancellable([sys.executable, '-c', parent], '', 10, {}, event)
    trigger.join(timeout=2)
    assert result.returncode != 0 and 'Hub 已明确撤销' in result.stderr
    assert len(result.stdout) == 200000 and marker.exists()
    child_pid = int(marker.read_text())
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, child_pid)
        if handle:
            try:
                exit_code = ctypes.c_ulong()
                assert kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                assert exit_code.value != 259  # STILL_ACTIVE
            finally:
                kernel.CloseHandle(handle)
    else:
        state = Path(f'/proc/{child_pid}/stat')
        assert not state.exists() or state.read_text().split()[2] == 'Z'


def test_cancellable_process_completes_normally_when_network_failure_has_no_cancel(tmp_path):
    runner = CodexExecRunner(config_for(tmp_path))
    event = threading.Event()
    result = runner._run_cancellable([sys.executable, '-c',
        'import sys,time;value=sys.stdin.read();time.sleep(1.1);print(value);print("err",file=sys.stderr)'],
        'input sent once', 5, {}, event)
    assert result.returncode == 0 and result.stdout.strip() == 'input sent once'
    assert result.stderr.strip() == 'err'
