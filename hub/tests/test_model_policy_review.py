"""Independent regression for model-choice races and planning cost deduplication."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from codex_hub.agents import AgentStore
from codex_hub.device_capabilities import save_device_capabilities
from codex_hub.execution_leases import ExecutionStore
from codex_hub.model_policy import save_policy, task_choice
from test_agents import token_headers
from test_model_policy import setup_team


CHOICE = {'model':'gpt-6-sol','reasoning_effort':'medium','reason':'一般实现，综合能力与成本'}


def controller_source(repo, project, device, store, controller):
    task = repo.create_task(project['id'],'安排本轮工作',assigned_agent_id=controller['id'])
    task = store.route(task['id'],{'action':'new','title':'模型分配验证','reason':'测试'},'codex_cli')
    executions = ExecutionStore(repo)
    prepared = executions.acquire(task['id'],device['id'],'controller-model-review-run')['execution']
    started = executions.start(task['id'],device['id'],prepared['generation'],prepared['run_key'])['execution']
    return task, {'generation':started['generation'],'run_key':started['run_key']}


def test_task_model_post_rechecks_bound_device_inside_write_transaction(client, repo, settings, monkeypatch):
    project, device, store, controller, worker, models = setup_team(repo,settings)
    other = repo.create_device('另一个执行连接')
    repo.add_project_member(project['id'],other['id'],'write')
    save_device_capabilities(settings.data_dir,other['id'],{'codex':{'models':[models[0]]}})
    task = repo.create_task(project['id'],'转交到另一执行连接',assigned_agent_id=worker['id'])
    original = AgentStore.get
    changed = []
    def rebind_after_initial_read(self, project_id, agent_id):
        result = original(self,project_id,agent_id)
        if agent_id == worker['id'] and not changed:
            changed.append(True)
            repo.connection.execute('UPDATE tasks SET assigned_device_id=? WHERE id=?',(other['id'],task['id']))
            repo.connection.commit()
        return result
    monkeypatch.setattr(AgentStore,'get',rebind_after_initial_read)
    response = client.post(f"/api/projects/{project['slug']}/tasks/{task['id']}/model-plan",
                           headers=token_headers(repo,device),json=CHOICE)
    assert response.status_code in {403,409}, 'Old device must not choose a model after the task moved.'
    assert task_choice(repo.connection,task['id']) is None


def test_delegate_retry_preserves_original_task_after_user_model_policy_changes(client, repo, settings):
    project, device, store, controller, worker, models = setup_team(repo,settings)
    source, execution = controller_source(repo,project,device,store,controller)
    path = f"/api/projects/{project['slug']}/tasks/{source['id']}/delegate"
    data = {'assigned_agent_id':worker['id'],'title':'独立交付','description':'只创建一次','request_key':'model-review-delegate-001',
            'execution':execution,'model_selection':CHOICE}
    headers = token_headers(repo,device)
    first = client.post(path,headers=headers,json=data)
    assert first.status_code == 201
    saved = task_choice(repo.connection,first.json()['task']['id'])
    save_policy(repo.connection,worker,settings.data_dir,mode='manual',model='gpt-5.6-sol',reasoning_effort='high')
    retry = client.post(path,headers=headers,json=data)
    assert retry.status_code == 201, 'An unchanged request must recover its existing task after later policy changes.'
    assert retry.json()['task']['id'] == first.json()['task']['id']
    assert task_choice(repo.connection,first.json()['task']['id']) == saved


def test_delegate_respects_user_override_committed_before_dispatch_write(client, repo, settings, monkeypatch):
    project, device, store, controller, worker, models = setup_team(repo,settings)
    source, execution = controller_source(repo,project,device,store,controller)
    original = AgentStore.dispatch
    def update_user_before_transaction(self,*args,**kwargs):
        save_policy(repo.connection,worker,settings.data_dir,mode='manual',model='gpt-5.6-sol',reasoning_effort='high')
        return original(self,*args,**kwargs)
    monkeypatch.setattr(AgentStore,'dispatch',update_user_before_transaction)
    response = client.post(f"/api/projects/{project['slug']}/tasks/{source['id']}/delegate",headers=token_headers(repo,device),json={
        'assigned_agent_id':worker['id'],'title':'以最新用户设置执行','description':'验证事务边界','request_key':'model-review-user-race',
        'execution':execution,'model_selection':CHOICE})
    assert response.status_code == 201
    frozen = task_choice(repo.connection,response.json()['task']['id'])
    assert frozen['model'] == 'gpt-5.6-sol' and frozen['source']=='user', 'User override must win when committed before task creation.'


def test_cancelled_unplanned_task_does_not_offer_paid_planning_context(client, repo, settings):
    project, device, store, controller, worker, models = setup_team(repo,settings)
    task = repo.create_task(project['id'],'取消前尚未规划',assigned_agent_id=worker['id'])
    repo.update_task_status(task['id'],'cancelled','用户取消')
    response = client.get(f"/api/projects/{project['slug']}/tasks/{task['id']}/model-plan",headers=token_headers(repo,device))
    assert response.status_code == 409, 'GET must stop stale inbox workers before they pay for cancelled-task planning.'
    assert task_choice(repo.connection,task['id']) is None


def test_concurrent_planner_attempts_only_invoke_decider_once(tmp_path):
    from codex_bridge.model_allocator import allocate_task_model
    models = [{'model':'gpt-6-sol','supportedReasoningEfforts':[{'reasoningEffort':'medium'}]}]
    selected = []
    calls = []
    lock = threading.Lock()
    class Client:
        hub = 'https://synthetic.invalid/hub'
        def _json(self,method,path,data=None):
            with lock:
                if method=='GET':
                    return {'selection':selected[0] if selected else None,'models':models,'task':{'id':7}}
                if not selected:
                    selected.append(data)
                return {'selection':selected[0]}
    def decider(*args):
        calls.append(True)
        time.sleep(0.12)
        return CHOICE.copy()
    config = SimpleNamespace(codex_exec_output_dir=tmp_path)
    def allocate(_):
        try:
            return allocate_task_model(config,Client(),'model-team',7,decider=decider)
        except Exception as exc:
            return type(exc).__name__
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(allocate,range(2)))
    assert len(calls)==1, f'Concurrent workers made {len(calls)} paid decisions for one task.'
    assert results == [CHOICE,CHOICE]


def test_interrupted_planner_is_not_automatically_paid_for_again(tmp_path):
    from codex_bridge.agent_runtime import AgentDecisionError
    from codex_bridge.model_allocator import allocate_task_model
    calls = []
    class Client:
        hub = 'https://synthetic.invalid/hub'
        def _json(self,method,path,data=None):
            if method=='GET':
                return {'selection':None,'models':[{'model':'gpt-6-sol','supportedReasoningEfforts':[{'reasoningEffort':'medium'}]}],
                        'task':{'id':8}}
            return {'selection':data}
    def interrupted_decider(*args):
        calls.append(True)
        if len(calls)==1:
            raise SystemExit('synthetic interruption after paid planning was sent')
        return CHOICE.copy()
    config = SimpleNamespace(codex_exec_output_dir=tmp_path)
    with pytest.raises(SystemExit):
        allocate_task_model(config,Client(),'model-team',8,decider=interrupted_decider)
    with pytest.raises(AgentDecisionError):
        allocate_task_model(config,Client(),'model-team',8,decider=interrupted_decider)
    assert len(calls)==1
