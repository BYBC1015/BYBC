"""Fenced executions for durable agents; an expired run is not permission to rerun."""
from __future__ import annotations

import hashlib
import json
import re
import time
from contextlib import contextmanager


class ExecutionConflict(ValueError):
    pass


def initialize_execution_schema(db) -> None:
    db.executescript('''
      CREATE TABLE IF NOT EXISTS hub_task_executions (
        id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        generation INTEGER NOT NULL, run_key TEXT NOT NULL,
        device_id INTEGER NOT NULL REFERENCES devices(id),
        state TEXT NOT NULL, task_version INTEGER NOT NULL,
        lease_until REAL NOT NULL, created_at REAL NOT NULL, started_at REAL, completed_at REAL,
        result_digest TEXT NOT NULL DEFAULT '', result_entry_id INTEGER REFERENCES entries(id),
        detail TEXT NOT NULL DEFAULT '',
        UNIQUE(task_id,generation), UNIQUE(task_id,run_key)
      );
      CREATE INDEX IF NOT EXISTS hub_execution_task ON hub_task_executions(task_id,generation DESC);
    ''')
    db.commit()


def latest_execution(db, task_id: int) -> dict | None:
    row = db.execute('SELECT * FROM hub_task_executions WHERE task_id=? ORDER BY generation DESC LIMIT 1',
                     (task_id,)).fetchone()
    return dict(row) if row else None


def guard_legacy_task_write(db, task: dict | None, device_id: int | None) -> None:
    if not task or not task.get('assigned_agent_id') or device_id is None:
        return
    if task['status'] in {'done', 'cancelled'}:
        raise ExecutionConflict('任务已结束，执行连接不能用迟到回传覆盖当前状态。')
    if latest_execution(db, task['id']):
        raise ExecutionConflict('此任务使用执行凭据，请通过带代际校验的运行接口回传。')


def require_execution_fence(db, task: dict, device_id: int, execution: dict | None,
                            *, allow_unleased: bool = True) -> dict | None:
    current = latest_execution(db, task['id'])
    if not current and allow_unleased:
        if task['status'] in {'done', 'cancelled'}:
            raise ExecutionConflict('任务已结束，不能继续创建工作。')
        return None
    if (not current or not isinstance(execution, dict)
            or type(execution.get('generation')) is not int
            or current['generation'] != execution['generation']
            or current['run_key'] != execution.get('run_key')
            or current['device_id'] != device_id or task.get('assigned_device_id') != device_id
            or current['state'] != 'running' or current['lease_until'] <= time.time()
            or current['task_version'] != task['status_version'] or task['status'] != 'in_progress'):
        raise ExecutionConflict('执行凭据已失效或任务状态已改变，不能继续委派。')
    return current


class ExecutionStore:
    def __init__(self, repo, *, clock=time.time, lease_seconds: int = 150):
        self.repo, self.db, self.clock = repo, repo.connection, clock
        self.lease_seconds = max(30, min(600, lease_seconds))

    @contextmanager
    def transaction(self):
        # Repository status updates use nested savepoints; result state,
        # experience and the single visible entry commit as one transaction.
        if self.db.in_transaction:
            raise RuntimeError('Execution transaction requires a clean connection')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def _task(self, task_id, device_id, *, writable=True):
        task = self.repo.get_task(task_id)
        if (not task or not task.get('assigned_agent_id') or task.get('assigned_device_id') != device_id
                or not self.repo.can_device_write(task['project_id'], device_id)):
            raise ExecutionConflict('任务未分配给此专用执行连接。')
        return task

    def _public(self, execution):
        if not execution:
            return None
        return {key: execution[key] for key in ('generation', 'run_key', 'device_id', 'state', 'task_version',
            'lease_until', 'started_at', 'completed_at', 'result_entry_id', 'detail')}

    def _reply(self, task_id, execution):
        from .model_policy import task_choice
        return {'task': self.repo.get_task(task_id), 'execution': self._public(execution),
                'model_selection': task_choice(self.db, task_id)}

    def _fenced(self, task_id, device_id, generation, run_key):
        if type(generation) is not int or generation <= 0:
            raise ExecutionConflict('执行代际无效。')
        task = self._task(task_id, device_id)
        record = latest_execution(self.db, task_id)
        if (not record or record['generation'] != generation or record['run_key'] != run_key
                or record['device_id'] != device_id):
            raise ExecutionConflict('此执行已被新的代际取代，旧执行不能回传。')
        return task, record

    def acquire(self, task_id: int, device_id: int, run_key: str) -> dict:
        if not isinstance(run_key, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{16,100}', run_key):
            raise ValueError('执行请求标识必须稳定且有效。')
        with self.transaction():
            task = self._task(task_id, device_id)
            latest = latest_execution(self.db, task_id)
            from .work_delivery import dependency_ready
            if not dependency_ready(self.db, task):
                raise ExecutionConflict('原工作尚未结束，调整任务等待原执行结果')
            previous = self.db.execute('SELECT * FROM hub_task_executions WHERE task_id=? AND run_key=?',
                                        (task_id, run_key)).fetchone()
            if previous:
                if latest['id'] != previous['id'] or previous['device_id'] != device_id:
                    raise ExecutionConflict('该执行请求已经过期，不能复用旧凭据。')
                return self._reply(task_id, dict(previous))
            if task['status'] not in {'open', 'claimed'}:
                raise ExecutionConflict('任务并非待执行；已有工作需先核对结果，不能自动重跑。')
            if not latest and (task['status'] != 'open' or task.get('claimed_by_device_id') is not None):
                raise ExecutionConflict('旧连接已经领取此任务，缺少安全续跑凭据；等待核对，不能重复执行。')
            agent = self.db.execute('SELECT active FROM hub_agents WHERE id=?', (task['assigned_agent_id'],)).fetchone()
            if not agent or not agent['active']:
                raise ExecutionConflict('智能体已暂停，不能开始新执行。')
            if latest:
                if latest['state'] == 'prepared' and latest['lease_until'] <= self.clock():
                    self.db.execute("UPDATE hub_task_executions SET state='superseded',detail=? WHERE id=?",
                                    ('执行前凭据到期；没有发出开始执行许可。', latest['id']))
                elif latest['state'] == 'completed' and task['status_version'] > latest['task_version']:
                    pass  # A later explicit task reopen permits a new generation.
                else:
                    raise ExecutionConflict('此前执行仍持有凭据或结果不确定，等待核对；不会自动接管。')
            generation = (latest['generation'] if latest else 0) + 1
            self.db.execute('''INSERT INTO hub_task_executions
                (task_id,generation,run_key,device_id,state,task_version,lease_until,created_at,detail)
                VALUES (?,?,?,?,'prepared',?,?,?,?)''',
                (task_id, generation, run_key, device_id, task['status_version'],
                 self.clock() + self.lease_seconds, self.clock(), '尚未授权启动工作进程。'))
            return self._reply(task_id, latest_execution(self.db, task_id))

    def start(self, task_id, device_id, generation, run_key):
        with self.transaction():
            task, record = self._fenced(task_id, device_id, generation, run_key)
            if (record['state'] != 'prepared' or record['lease_until'] <= self.clock()
                    or task['status_version'] != record['task_version'] or task['status'] not in {'open','claimed'}):
                raise ExecutionConflict('开始执行许可不可重复领取，或任务已经改变；请核对当前运行。')
            agent = self.db.execute('SELECT active FROM hub_agents WHERE id=?', (task['assigned_agent_id'],)).fetchone()
            if not agent or not agent['active']:
                raise ExecutionConflict('智能体已暂停。')
            updated = self.repo.update_task_status(task_id, 'in_progress', '专用执行器已取得本次执行许可。',
                                                   actor_device_id=device_id, execution_fenced=True)
            self.db.execute("""UPDATE hub_task_executions SET state='running',started_at=?,lease_until=?,
                task_version=?,detail=? WHERE id=?""", (self.clock(), self.clock() + self.lease_seconds,
                updated['status_version'], '工作已开始；失联时必须先核对运行收据，不自动重跑。', record['id']))
            return self._reply(task_id, latest_execution(self.db, task_id))

    def renew(self, task_id, device_id, generation, run_key):
        with self.transaction():
            task, record = self._fenced(task_id, device_id, generation, run_key)
            if (record['state'] not in {'prepared','running'} or task['status_version'] != record['task_version']
                    or task['status'] in {'done','cancelled'}):
                raise ExecutionConflict('任务已改变或本次执行正在等待核对。')
            self.db.execute('UPDATE hub_task_executions SET lease_until=? WHERE id=?',
                            (self.clock() + self.lease_seconds, record['id']))
            return self._reply(task_id, latest_execution(self.db, task_id))

    def reconcile(self, task_id, device_id):
        with self.transaction():
            task = self._task(task_id, device_id)
            record = latest_execution(self.db, task_id)
            if (record and record['state'] == 'running' and record['lease_until'] <= self.clock()
                    and task['status_version'] == record['task_version'] and task['status'] == 'in_progress'):
                detail = '执行连接的任务续租已过期；工作可能仍在运行，等待原执行器收据核对，未转给其他电脑。'
                task = self.repo.update_task_status(task_id, 'blocked', detail, execution_fenced=True)
                self.db.execute("UPDATE hub_task_executions SET state='uncertain',task_version=?,detail=? WHERE id=?",
                                (task['status_version'], detail, record['id']))
                record = latest_execution(self.db, task_id)
            return self._reply(task_id, record)

    def complete(self, task_id, device_id, generation, run_key, *, status, summary, body, reflection):
        if (status not in {'review','blocked'} or not isinstance(summary, str) or len(summary) > 1000
                or not isinstance(body, str) or len(body) > 24000 or not isinstance(reflection, dict)
                or len(json.dumps(reflection, ensure_ascii=False)) > 20000):
            raise ValueError('执行结果格式无效或过长。')
        digest = hashlib.sha256(json.dumps([status,summary,body,reflection], ensure_ascii=False,
                                           sort_keys=True).encode()).hexdigest()
        with self.transaction():
            task, record = self._fenced(task_id, device_id, generation, run_key)
            if record['state'] == 'completed':
                if record['result_digest'] != digest:
                    raise ExecutionConflict('同一执行不能回传不同结果。')
                return {**self._reply(task_id, record), 'accepted': True, 'replayed': True}
            if (record['state'] not in {'running','uncertain'} or task['status_version'] != record['task_version']
                    or task['status'] not in {'in_progress','blocked'}):
                raise ExecutionConflict('任务已取消、结束或被更新；迟到结果保留本地，不覆盖当前状态。')
            from .work_delivery import artifact_links
            summary=artifact_links(self.db,task_id,generation,summary,'../attachments')
            updated = self.repo.update_task_status(task_id, status, summary, reflection=reflection,
                                                   actor_device_id=device_id, execution_fenced=True)
            device = self.repo.get_device(device_id)
            body=artifact_links(self.db,task_id,generation,body,'../attachments')
            entry = self.repo.create_entry(task['project_id'], device['label'], 'markdown', body,
                author_device_id=device_id, task_id=task_id, commit=False, execution_fenced=True,
                metadata_json=json.dumps({'execution': {'generation':generation,'run_key':run_key}}))
            self.db.execute('UPDATE attachments SET entry_id=? WHERE id IN (SELECT attachment_id FROM hub_task_artifacts WHERE task_id=? AND generation=?)',(entry['id'],task_id,generation))
            self.db.execute("""UPDATE hub_task_executions SET state='completed',completed_at=?,
                result_digest=?,result_entry_id=?,task_version=?,detail=? WHERE id=?""",
                (self.clock(), digest, entry['id'], updated['status_version'], '结果、经验与工作记录已原子保存。', record['id']))
            return {**self._reply(task_id, latest_execution(self.db, task_id)), 'accepted': True, 'replayed': False}
