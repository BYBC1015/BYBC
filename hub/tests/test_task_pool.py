"""Actual runner/API/lease flow with gated local child processes, no paid models."""
from pathlib import Path
from types import SimpleNamespace
import json
import subprocess
import sys
import time

import pytest

from codex_bridge.agent_worker import filter_agent_inbox
from codex_bridge.client import HubClient,HubClientError
from codex_bridge.codex_exec import CodexExecResult
from codex_bridge.runner import BridgeRunner
from codex_bridge.task_pool import DurableTaskPool,task_config
from codex_hub.agents import AgentStore
from codex_hub.execution_leases import ExecutionStore,ExecutionConflict
from test_agent_runtime import config_for,REFLECTION
from test_agents import token_headers
from test_execution_leases import lease_task,start,RESULT


class ApiClient(HubClient):
    def __init__(self,web,headers,task_id=None):
        super().__init__('https://hub.example','test');self.web=web;self.headers=headers;self.task_id=task_id
        self.pending_task_ids=lambda:set()

    def _request(self,method,path,body=None):
        response=self.web.request(method,path,json=body,headers=self.headers)
        if response.status_code>=400: raise HubClientError(response.text,status_code=response.status_code)
        return response.content

    def get_inbox(self):
        inbox=filter_agent_inbox(super().get_inbox(),self.pending_task_ids())
        inbox['tasks']=[task for task in inbox['tasks'] if task.get('assigned_agent_kind')=='worker' and (self.task_id is None or task['id']==self.task_id)]
        return inbox

    def execution_renewal_client(self):return self

    def get_task_attachments(self,slug,task_id):
        return self._json('GET',f'/api/projects/{slug}/tasks/{task_id}/attachments')


class GatedExecutor:
    def __init__(self,folder):self.folder=folder

    def run(self,slug,workspace,prompt_path,prompt,*,execution_context=None,**kwargs):
        task_id=execution_context['task_id']
        code='''from pathlib import Path
import sys,time,json,os
folder=Path(sys.argv[1]);task=sys.argv[2]
(folder/(task+'.started')).write_text(json.dumps({'pid':os.getpid(),'cwd':os.getcwd()}))
end=time.monotonic()+15
while not (folder/(task+'.release')).exists():
 if time.monotonic()>end: raise RuntimeError('test timeout')
 time.sleep(.01)
'''
        subprocess.run([sys.executable,'-c',code,str(self.folder),str(task_id)],cwd=workspace,
            capture_output=True,timeout=20,check=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        body='已交付\n<hub-reflection>'+json.dumps(REFLECTION)+'</hub-reflection>'
        return CodexExecResult(ok=True,command=[],returncode=0,stdout='',stderr='',last_message=body,output_path=prompt_path)


def until(pool,condition,timeout=10):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        pool.tick()
        if condition():return
        time.sleep(.02)
    raise AssertionError('pool did not reach expected state')


@pytest.mark.parametrize('isolated',[True,False])
def test_same_identity_independent_processes_overlap_and_shared_project_waits(client,repo,lease_task,tmp_path,monkeypatch,isolated):
    d=lease_task;agents=d['agents'];first=repo.get_task(d['task']['id'])
    second=repo.create_task(d['project']['id'],'Second independent deliverable',assigned_agent_id=d['worker']['id'])
    agents.route(second['id'],{'action':'new','title':'Second conversation','reason':'Independent work'},'codex_cli')
    if isolated:
        repo.connection.execute("UPDATE tasks SET execution_mode='isolated' WHERE id IN (?,?)",(first['id'],second['id']));repo.connection.commit()
    config=config_for(tmp_path);config.projects={d['project']['slug']:tmp_path/'project'}
    monkeypatch.setattr('codex_bridge.model_allocator.allocate_task_model',lambda *args:{})
    monkeypatch.setattr('codex_hub.model_policy.ensure_task_choice',lambda *args:{})
    headers=token_headers(repo,d['device']);made={}
    def factory(task):
        api=ApiClient(client,headers,task['id']);runner=BridgeRunner(task_config(config,task),client=api,executor=GatedExecutor(tmp_path))
        runner._next_capability_report=float('inf');made[task['id']]=runner
        api.pending_task_ids=lambda:{int(r['task_id']) for r in runner.state.task_deliveries.values()}
        return runner
    pool=DurableTaskPool(config,ApiClient(client,headers),runner_factory=factory)
    try:
        until(pool,lambda:(tmp_path/f'{first["id"]}.started').exists())
        if isolated:
            until(pool,lambda:(tmp_path/f'{second["id"]}.started').exists())
            assert repo.get_task(first['id'])['status']==repo.get_task(second['id'])['status']=='in_progress'
            assert made[first['id']].config.state_path!=made[second['id']].config.state_path
            one=json.loads((tmp_path/f'{first["id"]}.started').read_text())
            two=json.loads((tmp_path/f'{second["id"]}.started').read_text())
            assert one['cwd']!=two['cwd'] and one['pid']!=two['pid']
        else:
            for _ in range(4):pool.tick();time.sleep(.03)
            assert not (tmp_path/f'{second["id"]}.started').exists()
        (tmp_path/f'{first["id"]}.release').touch();(tmp_path/f'{second["id"]}.release').touch()
        until(pool,lambda:all(repo.get_task(t['id'])['status']=='review' for t in (first,second)))
        assert repo.connection.execute('SELECT COUNT(*) FROM hub_task_executions').fetchone()[0]==2
    finally:
        for task in (first,second):(tmp_path/f'{task["id"]}.release').touch()
        pool.close()


def test_uncertain_and_cancelled_writer_holds_resource_until_confirmed_exit(repo,lease_task):
    d=lease_task;store=ExecutionStore(repo);run=start(store,d)
    second=repo.create_task(d['project']['id'],'Another shared write',assigned_agent_id=d['worker']['id'])
    with pytest.raises(ExecutionConflict,match='资源'):
        store.acquire(second['id'],d['device']['id'],'shared-writer-next-001')
    repo.connection.execute("UPDATE hub_task_executions SET state='uncertain' WHERE task_id=?",(d['task']['id'],));repo.connection.commit()
    repo.update_task_status(d['task']['id'],'cancelled','User cancelled')
    with pytest.raises(ExecutionConflict,match='资源'):
        store.acquire(second['id'],d['device']['id'],'shared-writer-next-001')
    store.acknowledge_stopped(d['task']['id'],d['device']['id'],run['generation'],run['run_key'])
    assert store.acquire(second['id'],d['device']['id'],'shared-writer-next-001')['execution']['state']=='prepared'


def test_external_resource_lock_applies_even_to_isolated_tasks_and_other_devices(repo,lease_task):
    d=lease_task;other=repo.create_device('Other private device');repo.add_project_member(d['project']['id'],other['id'],'write')
    agent=d['agents'].create(d['project']['id'],'Another executor','Same external timeline',device_id=other['id'])
    second=repo.create_task(d['project']['id'],'External tool work',assigned_agent_id=agent['id'])
    repo.connection.execute('UPDATE tasks SET execution_mode=?,resource_keys_json=?',('isolated',json.dumps(['resolve:shared-project'])))
    repo.connection.commit();start(ExecutionStore(repo),d)
    with pytest.raises(ExecutionConflict,match='资源'):
        ExecutionStore(repo).acquire(second['id'],other['id'],'cross-device-resource-001')


def test_one_task_error_does_not_stop_another_identity_or_discard_receipt(tmp_path):
    config=config_for(tmp_path);completed=[]
    tasks=[{'id':i,'project_slug':'agent-test','assigned_agent_id':7,'execution_mode':'isolated'} for i in (1,2)]
    def factory(task):
        def run():
            if task['id']==1:raise RuntimeError('task-specific failure')
            completed.append(2);return SimpleNamespace(messages=[])
        return SimpleNamespace(process_once=run,state=SimpleNamespace(task_deliveries={}))
    pool=DurableTaskPool(config,SimpleNamespace(get_inbox=lambda:{'tasks':tasks}),runner_factory=factory)
    try:until(pool,lambda:completed==[2])
    finally:pool.close()


def test_waiting_high_priority_resource_does_not_starve_independent_work(tmp_path):
    config=config_for(tmp_path);completed=[]
    tasks=[{'id':i,'project_slug':'agent-test','assigned_agent_id':7,'execution_mode':'isolated',
            'priority':'high' if i==1 else 'normal'} for i in (1,2)]
    def factory(task):
        def run():
            if task['id']==1:return SimpleNamespace(messages=[],pending_tasks=[1],executed_codex=[])
            completed.append(2);return SimpleNamespace(messages=[])
        return SimpleNamespace(process_once=run,state=SimpleNamespace(task_deliveries={}))
    pool=DurableTaskPool(config,SimpleNamespace(get_inbox=lambda:{'tasks':tasks}),capacity=1,runner_factory=factory)
    try:until(pool,lambda:completed==[2])
    finally:pool.close()


def test_pool_restart_recovers_lost_result_ack_without_reexecuting(client,repo,lease_task,tmp_path,monkeypatch):
    d=lease_task;task_id=d['task']['id'];config=config_for(tmp_path)
    config.projects={d['project']['slug']:tmp_path/'project'}
    monkeypatch.setattr('codex_bridge.model_allocator.allocate_task_model',lambda *args:{})
    monkeypatch.setattr('codex_hub.model_policy.ensure_task_choice',lambda *args:{})
    headers=token_headers(repo,d['device']);created=[];lose_ack=[True];result_sent=[False]
    class LostAck(ApiClient):
        def complete_task_execution(self,*args,**kwargs):
            result=super().complete_task_execution(*args,**kwargs)
            result_sent[0]=True
            if lose_ack[0]:raise HubClientError('acknowledgement was lost')
            return result
        def reconcile_task_execution(self,*args,**kwargs):
            if lose_ack[0] and result_sent[0]:raise HubClientError('connection remains offline until restart')
            return super().reconcile_task_execution(*args,**kwargs)
    def factory(task):
        api=LostAck(client,headers,task['id'])
        runner=BridgeRunner(task_config(config,task),client=api,executor=GatedExecutor(tmp_path))
        runner._next_capability_report=float('inf');created.append(runner)
        api.pending_task_ids=lambda:{int(r['task_id']) for r in runner.state.task_deliveries.values()}
        return runner
    (tmp_path/f'{task_id}.release').touch()
    pool=DurableTaskPool(config,ApiClient(client,headers),runner_factory=factory)
    try:until(pool,lambda:repo.get_task(task_id)['status']=='review')
    finally:pool.close()
    assert created[0].state.task_deliveries
    first_process=(tmp_path/f'{task_id}.started').read_text()
    lose_ack[0]=False
    # No longer in the fresh inbox: recovery depends on durable task metadata.
    pool=DurableTaskPool(config,SimpleNamespace(get_inbox=lambda:{'tasks':[]}),runner_factory=factory)
    try:until(pool,lambda:len(created)==2 and not created[1].state.task_deliveries)
    finally:pool.close()
    assert (tmp_path/f'{task_id}.started').read_text()==first_process
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_task_executions WHERE task_id=?',(task_id,)).fetchone()[0]==1
