"""Reversible task-list organization, independent of execution and chat history."""
import time
from pathlib import Path
from .execution_leases import ExecutionConflict,latest_execution


def initialize_task_board_schema(db):
    columns={row['name'] for row in db.execute('PRAGMA table_info(tasks)')}
    if 'board_state' not in columns:
        db.execute("ALTER TABLE tasks ADD COLUMN board_state TEXT NOT NULL DEFAULT 'auto'")
    if 'board_archive_reason' not in columns:
        db.execute("ALTER TABLE tasks ADD COLUMN board_archive_reason TEXT NOT NULL DEFAULT ''")
    db.commit()


def active_sql(alias='t'):
    # A cancellation is not proof that its process stopped. Keep uncertain or
    # live execution visible until the worker acknowledges the stop.
    # Chat turns retain durable execution receipts and remain in the transcript,
    # but do not masquerade as production work. Explicit restore still exposes one.
    return f"""({alias}.board_state='active' OR ({alias}.execution_route!='chat' AND (
        EXISTS (SELECT 1 FROM hub_task_executions board_run
        WHERE board_run.task_id={alias}.id AND board_run.state IN ('running','uncertain')
        AND board_run.generation=(SELECT MAX(board_last.generation) FROM hub_task_executions board_last WHERE board_last.task_id={alias}.id))
        OR ({alias}.board_state='auto'
        AND {alias}.status NOT IN ('done','cancelled')
        AND NOT ({alias}.status='review' AND EXISTS
          (SELECT 1 FROM hub_agents board_agent WHERE board_agent.id={alias}.assigned_agent_id AND board_agent.kind='controller'))))))"""


def can_archive(task, execution):
    if task['status'] not in {'review','blocked','done','cancelled'} or task.get('coordination_state')=='waiting':
        return False
    return not execution or execution['state'] in {'completed','yielded','stopped','superseded'} or (
        execution['state']=='prepared' and execution['lease_until']<=time.time())


def _web_delivery(db, task, execution, files_root):
    """Require real, published files for the latest completed execution."""
    if (not files_root or task['status'] not in {'review','done'}
            or task.get('coordination_state')=='waiting' or not execution
            or execution['state']!='completed' or not execution.get('result_entry_id')):
        return False
    root=Path(files_root).resolve()
    rows=db.execute('''SELECT a.stored_relpath,a.byte_size FROM hub_task_artifacts ar
        JOIN attachments a ON a.id=ar.attachment_id
        JOIN entries e ON e.id=a.entry_id
        WHERE ar.task_id=? AND ar.generation=? AND a.project_id=? AND a.byte_size>0
        AND e.id=? AND e.task_id=? AND e.deleted_at IS NULL''',
        (task['id'],execution['generation'],task['project_id'],execution['result_entry_id'],task['id'])).fetchall()
    for row in rows:
        try:
            path=(root/row['stored_relpath']).resolve()
            if path.is_relative_to(root) and path.is_file() and path.stat().st_size==row['byte_size']:
                return True
        except OSError:
            continue
    return False


def archive_completed_delivery(repo, task_id, files_root):
    """Organize only at committed-result events, inside the caller transaction.

    A pinned/restored task stays active. This changes neither acceptance nor
    execution status, and never infers replacement from similar titles.
    """
    db=repo.connection
    if not db.in_transaction:
        raise RuntimeError('Automatic organization requires a result transaction')
    task=repo.get_task(task_id)
    if not task or not _web_delivery(db,task,latest_execution(db,task_id),files_root):
        return []
    archived=[]
    if task['board_state']=='auto' and task['status']=='review':
        db.execute("UPDATE tasks SET board_state='archived',board_archive_reason='delivered' WHERE id=?",(task_id,))
        archived.append(task_id)
    # Only a proven queued revision relationship can retire an obsolete block.
    # Follow the explicit chain; unrelated repeated titles are never candidates.
    visited={task_id};pending=[task_id]
    while pending and len(visited)<=64:
        successor=pending.pop()
        rows=db.execute("SELECT DISTINCT target_task_id FROM hub_task_amendments WHERE next_task_id=? AND next_task_id!=target_task_id AND mode='queued'",(successor,)).fetchall()
        for row in rows:
            parent_id=row['target_task_id']
            if parent_id in visited:continue
            visited.add(parent_id)
            parent=repo.get_task(parent_id)
            if not parent or (parent['project_id'],parent['conversation_id'])!=(task['project_id'],task['conversation_id']):continue
            if not can_archive(parent,latest_execution(db,parent_id)):continue
            pending.append(parent_id)
            if parent['status']=='blocked' and parent['board_state']=='auto':
                db.execute("UPDATE tasks SET board_state='archived',board_archive_reason=? WHERE id=?",('superseded:'+str(task_id),parent_id))
                archived.append(parent_id)
    return archived


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
        db.execute('UPDATE tasks SET board_state=?,board_archive_reason=? WHERE id=?',
            ('active' if action=='restore' else 'archived','' if action=='restore' else 'manual',task_id))
    return repo.get_task(task_id)
