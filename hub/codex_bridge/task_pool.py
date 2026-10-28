"""Bounded task concurrency with durable per-task receipts and isolated outputs."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import json
import os
import re
import time

from .client import HubClient
from .runner import BridgeRunner
from .state import BridgeState


def needs_recovery(receipt):
    return receipt.get('stage')!='agent_result_rejected' and not (
        receipt.get('stage')=='agent_decision_blocked' and receipt.get('status_reported'))


def task_config(config, task):
    task_id=int(task['id']);slug=task['project_slug']
    root=config.state_path.parent/'task-runs'/str(task_id)
    projects=dict(config.projects);project_root=config.project_root
    if task.get('execution_mode')=='isolated':
        project_root=root/'workspace';projects={slug:project_root}
    return replace(config,state_path=root/'state.json',prompt_dir=root/'prompts',codex_exec_output_dir=root/'outputs',
        projects=projects,project_root=project_root,auto_create_missing_projects=True,
        backend_options={**config.backend_options,'agent_network_access':True,
                         'project_session_root':str(config.backend_options.get('project_session_root') or config.state_path.parent/'project-sessions')})


def resource_keys(config,task):
    keys=set(json.loads(task.get('resource_keys_json') or '[]'))
    if task.get('execution_mode','shared')=='shared':
        slug=task['project_slug'];path=config.projects.get(slug)
        keys.add('workspace:'+os.path.normcase(str(Path(path).resolve())) if path else 'project:'+slug)
    return keys


class SingleTaskClient(HubClient):
    def __init__(self, config, task_id):
        super().__init__(config.hub,config.token,timeout_seconds=config.hub_request_timeout_seconds,
            get_retry_attempts=config.hub_get_retry_attempts,retry_backoff_seconds=config.hub_retry_backoff_seconds,source_ip=config.hub_source_ip)
        self.task_id=task_id;self.pending_task_ids=lambda:set()

    def get_inbox(self):
        from .agent_worker import filter_agent_inbox
        inbox=filter_agent_inbox(super().get_inbox(),self.pending_task_ids())
        inbox['tasks']=[task for task in inbox['tasks'] if task['id']==self.task_id]
        return inbox


class DurableTaskPool:
    def __init__(self, config, client, *, capacity=2, runner_factory=None):
        self.config=config;self.client=client;self.capacity=max(1,min(4,int(capacity)))
        self.executor=ThreadPoolExecutor(max_workers=self.capacity,thread_name_prefix='hub-task')
        self.runners={};self.metadata={};self.active={};self.retry_after={};self.runner_factory=runner_factory or self._runner
        root=config.state_path.parent/'task-runs'
        for path in root.glob('*/task.json'):
            try:
                task=json.loads(path.read_text(encoding='utf-8'))
                if type(task.get('id')) is not int or str(task['id'])!=path.parent.name or not re.fullmatch(r'[A-Za-z0-9_-]+',task.get('project_slug','')):
                    continue
                state=BridgeState.load(path.parent/'state.json')
                if any(needs_recovery(r) for r in state.task_deliveries.values()):
                    self.metadata[task['id']]=task
            except (OSError,ValueError):
                # A corrupt state is never grounds to launch unverified old work.
                continue

    def _runner(self, task):
        config=task_config(self.config,task);client=SingleTaskClient(config,task['id'])
        runner=BridgeRunner(config,client=client)
        # The dedicated recovery/heartbeat lane reports the device catalog once;
        # creating a task must not re-query the same model inventory.
        runner._next_capability_report=float('inf')
        client.pending_task_ids=lambda:{int(r['task_id']) for r in runner.state.task_deliveries.values() if isinstance(r.get('task_id'),int)}
        return runner

    def tick(self):
        messages=[]
        for task_id,(future,keys) in list(self.active.items()):
            if not future.done():continue
            try:
                result=future.result();messages.extend(result.messages)
                if getattr(result,'pending_tasks',None) and not getattr(result,'executed_codex',None):
                    self.retry_after[task_id]=time.monotonic()+15
            except Exception as exc:
                messages.append(f'任务 #{task_id} 本轮未完成（{type(exc).__name__}），保留收据；其他任务继续。')
                self.retry_after[task_id]=time.monotonic()+15
            del self.active[task_id]
        inbox=self.client.get_inbox()
        fresh={task['id']:task for task in inbox.get('tasks',[]) if task.get('assigned_agent_kind','worker')=='worker' and task.get('coordination_state')!='waiting'}
        for task_id,runner in list(self.runners.items()):
            if task_id not in fresh and task_id not in self.active and not any(needs_recovery(r) for r in runner.state.task_deliveries.values()):
                del self.runners[task_id];self.metadata.pop(task_id,None);self.retry_after.pop(task_id,None)
        # Recovery receipts also need service when a cancelled task vanished from inbox.
        candidates=dict(fresh)
        for task_id,task in self.metadata.items():
            if task_id in self.active:continue
            runner=self.runners.get(task_id)
            if task_id not in candidates and (runner is None or any(needs_recovery(r) for r in runner.state.task_deliveries.values())):
                candidates[task_id]=task
        for task_id,task in sorted(candidates.items(),key=lambda item:({'high':0,'normal':1,'low':2}.get(item[1].get('priority'),1),item[0])):
            if len(self.active)>=self.capacity:break
            if task_id in self.active:continue
            if self.retry_after.get(task_id,0)>time.monotonic():continue
            keys=resource_keys(self.config,task)
            if any(keys & held for _,held in self.active.values()):continue
            runner=self.runners.get(task_id)
            if runner is None:
                runner=self.runner_factory(task);self.runners[task_id]=runner
            self.metadata[task_id]=task
            path=task_config(self.config,task).state_path.parent/'task.json'
            path.parent.mkdir(parents=True,exist_ok=True)
            saved={key:task[key] for key in ('id','project_id','project_slug','assigned_agent_id','assigned_agent_kind','assigned_device_id','conversation_id','execution_mode','resource_keys_json','execution_route','execution_route_reason') if key in task}
            temp=path.with_suffix('.tmp');temp.write_text(json.dumps(saved),encoding='utf-8');temp.replace(path)
            self.active[task_id]=(self.executor.submit(runner.process_once),keys)
        return messages

    def close(self):
        self.executor.shutdown(wait=True,cancel_futures=True)
