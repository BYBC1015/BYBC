from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
from types import SimpleNamespace

from codex_bridge.provider_health import health_path, outcome, read_health, record_outcome
from codex_hub.model_catalog import normalize_device_capabilities
from codex_hub.execution_context import annotate_execution, provider_observation


def result(event, *, ok=False, model='gpt-6-astra'):
    return SimpleNamespace(ok=ok, stdout=json.dumps(event), command=['codex', '--model', model, 'exec'])


def test_only_terminal_protocol_outcomes_count_and_success_restores(tmp_path):
    config = SimpleNamespace(backend_options={'provider_health_path': str(tmp_path/'health.json')})
    for event in [dict(type='item.completed', item=dict(text='401 network is unreachable')),
                  dict(type='error', message='stream disconnected, retrying'),
                  dict(type='turn.failed', error=dict(message='File not found'))]:
        assert outcome(result(event)) is None
    failed = result(dict(type='turn.failed', error=dict(message='error sending request (https://private/?token=never-upload)')))
    record_outcome(config, failed)
    assert read_health(config)['models'][0]['state'] == 'unreachable'
    assert 'never-upload' not in ''.join(p.read_text() for p in health_path(config).glob('*.json'))
    record_outcome(config, result(dict(type='turn.completed'), ok=True))
    assert read_health(config)['models'][0]['state'] == 'reachable'
    assert outcome(result(dict(type='turn.completed'), ok=False)) is None
    cached = result(dict(type='turn.completed'), ok=True)
    cached.command = []
    record_outcome(config, cached)
    assert len(read_health(config)['models']) == 1


def test_parallel_model_observations_are_not_lost(tmp_path):
    config = SimpleNamespace(backend_options={'provider_health_path': str(tmp_path/'health.json')})
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda i: record_outcome(config, result(dict(type='turn.completed'), ok=True, model=f'model-{i}')), range(12)))
    assert len(read_health(config)['models']) == 12
    assert not list(tmp_path.rglob('*.tmp'))


def test_late_publication_cannot_replace_newer_observation(tmp_path, monkeypatch):
    config = SimpleNamespace(backend_options={'provider_health_path': str(tmp_path/'health.json')})
    monkeypatch.setattr('codex_bridge.provider_health.time.time_ns', lambda: 1793400002000000000)
    record_outcome(config, result(dict(type='turn.completed'), ok=True))
    monkeypatch.setattr('codex_bridge.provider_health.time.time_ns', lambda: 1793400001000000000)
    record_outcome(config, result(dict(type='turn.failed', error=dict(code='connection_error'))))
    assert read_health(config)['models'][0]['state'] == 'reachable'


def test_independent_processes_share_health_without_overwriting(tmp_path):
    script = '''
import json, sys
from types import SimpleNamespace
from codex_bridge.provider_health import record_outcome
config = SimpleNamespace(backend_options={'provider_health_path': sys.argv[1]})
for index in range(8):
    result = SimpleNamespace(ok=True, stdout=json.dumps({'type':'turn.completed'}),
        command=['codex','--model',sys.argv[2]+'-'+str(index),'exec'])
    record_outcome(config, result)
'''
    path = str(tmp_path/'health.json')
    processes = [subprocess.Popen([sys.executable, '-c', script, path, name],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE) for name in ('first', 'second')]
    try:
        for process in processes:
            _, error = process.communicate(timeout=15)
            assert process.returncode == 0, error.decode(errors='replace')
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    assert len(read_health(SimpleNamespace(backend_options={'provider_health_path':path}))['models']) == 16


def test_health_is_sanitized_model_specific_and_expires():
    now = datetime.now(timezone.utc)
    row = dict(model='gpt-6-astra', state='unreachable', reason='transport_error', observed_at=now.isoformat(), secret='nope')
    cap = normalize_device_capabilities(dict(provider_health=dict(source='cli-outcome', models=[row]), token='nope'))
    assert 'nope' not in json.dumps(cap)
    assert provider_observation(cap, 'gpt-6-astra', now=now)['state'] == 'unreachable'
    assert provider_observation(cap, 'gpt-6.1-sol', now=now) is None
    assert provider_observation(cap, now=now+timedelta(minutes=11)) is None
    assert provider_observation(cap, now=now-timedelta(seconds=1)) is None
    bad = normalize_device_capabilities(dict(provider_health=dict(source='cli-outcome', models=[dict(row, observed_at='bad'), dict(row, state=[])])))
    assert bad['provider_health']['models'] == []


def test_online_and_model_failure_do_not_move_or_block_task():
    stamp = datetime.now(timezone.utc).isoformat()
    cap = {8: dict(provider_health=dict(source='cli-outcome', models=[dict(model='gpt-6-astra',
        state='unreachable', reason='transport_error', observed_at=stamp)]))}
    members = [dict(device_id=8, is_agent_online=True, agent_tasks_only=True),
               dict(device_id=3, is_agent_online=True, agent_tasks_only=True)]
    agents = [dict(id=1, device_id=8, active=True, can_dispatch=True)]
    tasks = [dict(id=1, assigned_agent_id=1, assigned_device_id=8, status='open', model_selection=dict(model='gpt-6-astra')),
             dict(id=2, assigned_agent_id=1, assigned_device_id=8, status='in_progress', model_selection=dict(model='gpt-6.1-sol'))]
    annotate_execution(agents, tasks, members, cap)
    assert agents[0]['connection_state'] == 'connected'
    assert agents[0]['provider_state'] == 'unreachable'
    assert '模型网络连接异常' in tasks[0]['execution_hint']
    assert '模型网络连接异常' not in tasks[1]['execution_hint']
    assert [(t['assigned_device_id'], t['status']) for t in tasks] == [(8, 'open'), (8, 'in_progress')]


def test_capability_report_refreshes_on_outcome_without_waiting_five_minutes(tmp_path):
    from codex_bridge.config import BridgeConfig
    from codex_bridge.runner import BridgeRunner
    config = BridgeConfig(hub='https://hub.example', token='fixture', state_path=tmp_path/'state.json',
        prompt_dir=tmp_path/'prompts', backend_options={'provider_health_path':str(tmp_path/'health.json')})
    reports = []
    client = SimpleNamespace(post_device_capabilities=lambda payload: reports.append(payload))
    runner = BridgeRunner(config, client=client)
    runner._model_catalog = SimpleNamespace(get_catalog=lambda: {'models': []})
    runner._report_device_capabilities()
    record_outcome(config, result(dict(type='turn.completed'), ok=True))
    runner._report_device_capabilities()
    assert len(reports) == 1  # bounded ten-second announcement rate
    runner._health_report_retry_at = 0
    runner._report_device_capabilities()
    assert len(reports) == 2
    assert reports[-1]['provider_health']['models'][0]['state'] == 'reachable'
    runner._next_capability_report = float('inf')  # pooled task runners never announce
    runner._health_report_retry_at = 0
    runner._report_device_capabilities()
    assert len(reports) == 2
