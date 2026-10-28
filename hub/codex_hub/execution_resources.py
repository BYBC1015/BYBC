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
    if task.get('assigned_agent_kind')=='controller':
        if task.get('controller_read_only'): return set()
        return {'controller-room:'+str(task['project_id'])+':'+str(task.get('conversation_id') or 'unrouted')}
    mode,resources=normalize_scope(task.get('execution_mode','shared'),json.loads(task.get('resource_keys_json') or '[]'))
    keys=set(resources)
    if mode=='shared': keys.add('project:'+str(task['project_id']))
    return keys


def resource_conflict(db, task, clock):
    from .execution_leases import ExecutionConflict
    agent=db.execute('SELECT kind FROM hub_agents WHERE id=?',(task['assigned_agent_id'],)).fetchone()
    keys=task_resources({**task,'assigned_agent_kind':agent['kind']})
    if not keys:return
    if agent['kind']=='controller':
        earlier=db.execute('''SELECT t.id FROM tasks t JOIN hub_agents a ON a.id=t.assigned_agent_id
            WHERE t.project_id=? AND t.conversation_id IS ? AND t.id<? AND a.kind='controller'
              AND t.controller_read_only=0 AND t.status IN ('open','claimed')
              AND t.coordination_state!='waiting'
            ORDER BY t.id LIMIT 1''',(task['project_id'],task.get('conversation_id'),task['id'])).fetchone()
        if earlier:
            raise ExecutionConflict(f"同一对话需按消息顺序处理；资源正等待较早的任务 #{earlier['id']}，不能先执行后续安排")
    # Cancellation/disconnection alone cannot prove the writer has stopped.
    rows=db.execute('''SELECT t.*,a.kind AS assigned_agent_kind,e.state,e.lease_until
        FROM hub_task_executions e JOIN tasks t ON t.id=e.task_id JOIN hub_agents a ON a.id=t.assigned_agent_id
        WHERE e.task_id!=? AND e.state IN ('prepared','running','uncertain')
        AND e.generation=(SELECT MAX(x.generation) FROM hub_task_executions x WHERE x.task_id=e.task_id)''',(task['id'],)).fetchall()
    for row in rows:
        if row['state']=='prepared' and row['lease_until']<=clock: continue
        if keys & task_resources(dict(row)):
            raise ExecutionConflict(f"共享工程或资源正由任务 #{row['id']} 使用，等待其执行结束；智能体仍可承担其他独立任务")
