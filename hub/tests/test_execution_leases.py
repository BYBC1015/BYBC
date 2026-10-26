"""Independent server regression for cancellation, generations and atomic results."""
import pytest

from codex_hub.agents import AgentStore
from codex_hub.execution_leases import ExecutionConflict, ExecutionStore, latest_execution
from test_agents import token_headers


RESULT = {'status':'review','summary':'回归完成，等待复核','body':'## 验证结果\n\n已修复并通过两项回归。',
          'reflection':{'summary':'修复已验证','worked':'依据复现步骤验证','failed':'','next_time':'保留回归用例'}}


@pytest.fixture
def lease_task(repo):
    device = repo.create_device(label='隔离测试执行连接')
    project = repo.create_project(slug='lease-testing',name='执行一致性',created_by_device_id=device['id'])
    agents = AgentStore(repo.connection)
    controller = agents.create(project['id'],'总控','统筹',kind='controller',device_id=device['id'])
    worker = agents.create(project['id'],'工程师','实现与验证',device_id=device['id'])
    task = repo.create_task(project['id'],'验证实际执行的一致性',assigned_agent_id=worker['id'])
    agents.route(task['id'],{'action':'new','title':'执行测试','reason':'独立测试'},'codex_cli')
    return {'device':device,'project':project,'task':task,'worker':worker,'controller':controller,'agents':agents}


def start(store, data, key='independent-execution-001'):
    prepared = store.acquire(data['task']['id'],data['device']['id'],key)['execution']
    return store.start(data['task']['id'],data['device']['id'],prepared['generation'],prepared['run_key'])['execution']


def test_cancelled_task_rejects_fenced_and_legacy_late_results(client, repo, lease_task):
    data = lease_task
    store = ExecutionStore(repo)
    execution = start(store,data)
    repo.update_task_status(data['task']['id'],'cancelled','用户已经取消')
    cancellation_memory = data['agents'].experiences(data['project']['id'],data['worker']['id'])
    headers = token_headers(repo,data['device'])
    path = f"/api/projects/lease-testing/tasks/{data['task']['id']}"
    result = client.post(path+'/execution/complete',headers=headers,json={**RESULT,**execution})
    assert result.status_code == 409
    legacy = client.post(path+'/status',headers=headers,json={'status':'review','result_summary':'迟到旧结果'})
    assert legacy.status_code in {409,422} and '任务已结束' in legacy.json()['detail']
    assert repo.get_task(data['task']['id'])['status'] == 'cancelled'
    assert repo.connection.execute('SELECT COUNT(*) FROM entries WHERE task_id=?',(data['task']['id'],)).fetchone()[0] == 0
    assert data['agents'].experiences(data['project']['id'],data['worker']['id']) == cancellation_memory
    assert latest_execution(repo.connection,data['task']['id'])['state'] == 'running'


def test_only_expired_prepared_execution_can_receive_new_generation(repo, lease_task):
    data = lease_task
    clock = [1000.0]
    store = ExecutionStore(repo,clock=lambda:clock[0],lease_seconds=30)
    first = store.acquire(data['task']['id'],data['device']['id'],'prepared-execution-first')['execution']
    assert store.acquire(data['task']['id'],data['device']['id'],first['run_key'])['execution'] == first
    with pytest.raises(ExecutionConflict):
        store.acquire(data['task']['id'],data['device']['id'],'prepared-execution-second')
    clock[0] += 31
    second = store.acquire(data['task']['id'],data['device']['id'],'prepared-execution-second')['execution']
    assert second['generation'] == first['generation']+1
    with pytest.raises(ExecutionConflict):
        store.start(data['task']['id'],data['device']['id'],first['generation'],first['run_key'])
    with pytest.raises(ExecutionConflict):
        store.acquire(data['task']['id'],data['device']['id'],first['run_key'])
    running = store.start(data['task']['id'],data['device']['id'],second['generation'],second['run_key'])['execution']
    assert running['state'] == 'running'
    with pytest.raises(ExecutionConflict):
        store.start(data['task']['id'],data['device']['id'],second['generation'],second['run_key'])
    states = [row[0] for row in repo.connection.execute('SELECT state FROM hub_task_executions ORDER BY generation')]
    assert states == ['superseded','running']


def test_running_expiry_never_grants_takeover_and_accepts_original_receipt(repo, lease_task):
    data = lease_task
    clock = [1000.0]
    store = ExecutionStore(repo,clock=lambda:clock[0],lease_seconds=30)
    execution = start(store,data)
    clock[0] += 31
    with pytest.raises(ExecutionConflict):
        store.acquire(data['task']['id'],data['device']['id'],'would-duplicate-real-work')
    state = store.reconcile(data['task']['id'],data['device']['id'])
    assert state['execution']['state'] == 'uncertain' and state['task']['status'] == 'blocked'
    with pytest.raises(ExecutionConflict):
        store.acquire(data['task']['id'],data['device']['id'],'still-cannot-duplicate-work')
    accepted = store.complete(data['task']['id'],data['device']['id'],execution['generation'],execution['run_key'],**RESULT)
    assert accepted['accepted'] and accepted['task']['status'] == 'review'
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_task_executions').fetchone()[0] == 1


def test_complete_commits_entry_status_and_experiences_atomically_and_retry_is_idempotent(repo, lease_task, monkeypatch):
    data = lease_task
    store = ExecutionStore(repo)
    execution = start(store,data)
    original = repo.create_entry
    def fail_entry(*args,**kwargs):
        raise RuntimeError('synthetic entry storage failure')
    monkeypatch.setattr(repo,'create_entry',fail_entry)
    with pytest.raises(RuntimeError,match='storage failure'):
        store.complete(data['task']['id'],data['device']['id'],execution['generation'],execution['run_key'],**RESULT)
    assert repo.get_task(data['task']['id'])['status'] == 'in_progress'
    assert latest_execution(repo.connection,data['task']['id'])['state'] == 'running'
    assert data['agents'].experiences(data['project']['id'],data['worker']['id']) == []
    assert data['agents'].experiences(data['project']['id'],data['controller']['id']) == []
    monkeypatch.setattr(repo,'create_entry',original)
    completed = store.complete(data['task']['id'],data['device']['id'],execution['generation'],execution['run_key'],**RESULT)
    replay = store.complete(data['task']['id'],data['device']['id'],execution['generation'],execution['run_key'],**RESULT)
    assert completed['replayed'] is False and replay['replayed'] is True
    assert completed['execution']['result_entry_id'] == replay['execution']['result_entry_id']
    with pytest.raises(ExecutionConflict):
        store.complete(data['task']['id'],data['device']['id'],execution['generation'],execution['run_key'],**{**RESULT,'body':'另一个结果'})
    assert repo.connection.execute('SELECT COUNT(*) FROM entries WHERE task_id=?',(data['task']['id'],)).fetchone()[0] == 1
    assert len(data['agents'].experiences(data['project']['id'],data['worker']['id'])) == 1
    assert len(data['agents'].experiences(data['project']['id'],data['controller']['id'])) == 1
    repo.update_task_status(data['task']['id'],'open','明确重新安排')
    next_run = store.acquire(data['task']['id'],data['device']['id'],'new-explicitly-reopened-run')['execution']
    assert next_run['generation'] == execution['generation']+1
    with pytest.raises(ExecutionConflict):
        store.complete(data['task']['id'],data['device']['id'],execution['generation'],execution['run_key'],**RESULT)


def test_legacy_claim_and_execution_acquire_cannot_own_same_task(repo, lease_task):
    data = lease_task
    store = ExecutionStore(repo)
    claimed = repo.claim_task(data['task']['id'],data['device']['id'])
    assert claimed and claimed['status'] == 'claimed'
    with pytest.raises(ExecutionConflict):
        store.acquire(data['task']['id'],data['device']['id'],'cannot-race-legacy-bridge')
    assert latest_execution(repo.connection,data['task']['id']) is None
    fresh = repo.create_task(data['project']['id'],'另一个真实执行',assigned_agent_id=data['worker']['id'])
    store.acquire(fresh['id'],data['device']['id'],'execution-owns-before-claim')
    assert repo.claim_task(fresh['id'],data['device']['id']) is None
    assert repo.get_task(fresh['id'])['status'] == 'open'
