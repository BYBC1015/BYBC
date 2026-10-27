"""Resource ownership belongs to an execution, never to an agent identity."""
import json
import re


def normalize_scope(mode='shared', resources=None):
    if not isinstance(mode,str) or mode not in {'shared','isolated'}:
        raise ValueError('execution_mode 只能是 shared 或 isolated')
    if resources is None: resources=[]
    if (not isinstance(resources,list) or len(resources)>12 or any(not isinstance(value,str) or
            not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{2,119}',value) for value in resources)):
        raise ValueError('resource_keys 需要最多 12 个稳定资源名称')
    return mode,sorted({value.lower().rstrip('/') for value in resources})


def task_resources(task):
    if task.get('assigned_agent_kind')=='controller': return set()
    mode,resources=normalize_scope(task.get('execution_mode','shared'),json.loads(task.get('resource_keys_json') or '[]'))
    keys=set(resources)
    if mode=='shared': keys.add('project:'+str(task['project_id']))
    return keys


def resource_conflict(db, task, clock):
    from .execution_leases import ExecutionConflict
    agent=db.execute('SELECT kind FROM hub_agents WHERE id=?',(task['assigned_agent_id'],)).fetchone()
    keys=task_resources({**task,'assigned_agent_kind':agent['kind']})
    if not keys:return
    # Cancellation/disconnection alone cannot prove the writer has stopped.
    rows=db.execute('''SELECT t.*,a.kind AS assigned_agent_kind,e.state,e.lease_until
        FROM hub_task_executions e JOIN tasks t ON t.id=e.task_id JOIN hub_agents a ON a.id=t.assigned_agent_id
        WHERE e.task_id!=? AND e.state IN ('prepared','running','uncertain')
        AND e.generation=(SELECT MAX(x.generation) FROM hub_task_executions x WHERE x.task_id=e.task_id)''',(task['id'],)).fetchall()
    for row in rows:
        if row['state']=='prepared' and row['lease_until']<=clock: continue
        if keys & task_resources(dict(row)):
            raise ExecutionConflict(f"共享工程或资源正由任务 #{row['id']} 使用，等待其执行结束；智能体仍可承担其他独立任务")
