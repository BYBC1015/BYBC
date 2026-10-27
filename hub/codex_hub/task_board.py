"""Reversible task-list organization, independent of execution and chat history."""
import time
from .execution_leases import ExecutionConflict,latest_execution


def initialize_task_board_schema(db):
    if 'board_state' not in {row['name'] for row in db.execute('PRAGMA table_info(tasks)')}:
        db.execute("ALTER TABLE tasks ADD COLUMN board_state TEXT NOT NULL DEFAULT 'auto'")
    db.commit()


def active_sql(alias='t'):
    # A cancellation is not proof that its process stopped. Keep uncertain or
    # live execution visible until the worker acknowledges the stop.
    return f"""(EXISTS (SELECT 1 FROM hub_task_executions board_run
        WHERE board_run.task_id={alias}.id AND board_run.state IN ('running','uncertain')
        AND board_run.generation=(SELECT MAX(board_last.generation) FROM hub_task_executions board_last WHERE board_last.task_id={alias}.id))
        OR {alias}.board_state='active' OR ({alias}.board_state='auto'
        AND {alias}.status NOT IN ('done','cancelled')
        AND NOT ({alias}.status='review' AND EXISTS
          (SELECT 1 FROM hub_agents board_agent WHERE board_agent.id={alias}.assigned_agent_id AND board_agent.kind='controller'))))"""


def can_archive(task, execution):
    if task['status'] not in {'review','blocked','done','cancelled'} or task.get('coordination_state')=='waiting':
        return False
    return not execution or execution['state'] in {'completed','yielded','stopped','superseded'} or (
        execution['state']=='prepared' and execution['lease_until']<=time.time())


def organize_task(repo,project_id,task_id,action):
    if action not in {'archive','restore','complete'}:raise ValueError('请选择归档、恢复或完成')
    db=repo.connection
    with db:
        db.execute('BEGIN IMMEDIATE')
        task=repo.get_task(task_id)
        if not task or task['project_id']!=project_id:raise ValueError('任务不属于当前项目')
        if action!='restore':
            if not can_archive(task,latest_execution(db,task_id)):
                raise ExecutionConflict('任务仍在运行或等待交接，请结束执行后再归档；当前记录已保留')
            if action=='complete':
                if task['status'] not in {'review','done'}:raise ValueError('只有待复核的交付可以确认完成；遇阻任务可直接归档')
                if task['status']!='done':repo.update_task_status(task_id,'done',execution_fenced=True)
        db.execute('UPDATE tasks SET board_state=? WHERE id=?',('active' if action=='restore' else 'archived',task_id))
    return repo.get_task(task_id)
