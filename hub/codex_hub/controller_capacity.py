"""Keep controller work durable across provider capacity failures."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .model_policy import available_models, reserve_policy, task_choice, validate_choice


WAIT_MESSAGE = '消息和附件已保存。总控正在等待模型额度恢复，可在设置中指定备用模型。'
RESERVE_MESSAGE = '主模型额度暂不可用，本项对话已排队交给设置中的备用模型。'
RETRY_MESSAGE = '本次请求在开始前被模型拒绝，原任务已保留，等待主模型重新领取。'


def capacity_label(status, summary):
    if status in {'open', 'claimed'}:
        return {WAIT_MESSAGE: '等待模型额度恢复', RESERVE_MESSAGE: '备用模型已排队',
                RETRY_MESSAGE: '等待主模型重试'}.get(summary, '')
    return ''


def scope(choice):
    model = choice.get('model', '')
    if model.startswith('hub-service/'):
        return f"{model}@{choice.get('service_revision', 0)}"
    return 'codex-cli'


def controller(db, task):
    return bool(task.get('assigned_agent_id') and db.execute(
        "SELECT 1 FROM hub_agents WHERE id=? AND kind='controller'", (task['assigned_agent_id'],)).fetchone())


def remember_primary(db, task_id, choice):
    db.execute('INSERT OR IGNORE INTO hub_controller_capacity_tasks VALUES (?,?)',
               (task_id, json.dumps(choice, ensure_ascii=False)))


def available_at(db, device_id, choice, now, capabilities):
    backend = scope(choice)
    row = db.execute('SELECT * FROM hub_controller_cooldowns WHERE device_id=? AND provider_scope=?',
                     (device_id, backend)).fetchone()
    if row:
        if row['retry_at'] > now:
            return row['retry_at']
        # Allow one new trial on a previously rejected connection. Other tasks
        # stay queued until that execution ends or a never-started lease expires.
        for run in db.execute('''SELECT c.selection_json,x.state,x.lease_until FROM hub_task_executions x
            JOIN tasks t ON t.id=x.task_id JOIN hub_agents a ON a.id=t.assigned_agent_id
            JOIN hub_task_model_choices c ON c.task_id=t.id WHERE x.device_id=? AND a.kind='controller'
            AND x.created_at>=? AND x.state IN ('running','prepared') AND x.lease_until>?''',
            (device_id, row['retry_at'], now)):
            if scope(json.loads(run['selection_json'])) == backend:
                return now + 15
    if backend == 'codex-cli' and choice.get('model') and capabilities:
        from .execution_context import provider_observation
        observed = provider_observation(capabilities, choice['model'], now=datetime.fromtimestamp(now, timezone.utc))
        if observed and observed['state'] == 'rate_limited':
            return datetime.fromisoformat(observed['observed_at'].replace('Z', '+00:00')).timestamp() + 600
    return 0


def selection(repo, task, data_dir, now, capabilities=None):
    """Read-only. Return the frozen primary, usable choice (or None), and earliest retry."""
    db = repo.connection
    saved = db.execute('SELECT primary_json FROM hub_controller_capacity_tasks WHERE task_id=?',
                       (task['id'],)).fetchone()
    primary = json.loads(saved[0]) if saved else task_choice(db, task['id']) or {}
    device_id = task['assigned_device_id']
    retry_at = available_at(db, device_id, primary, now, capabilities)
    models = None
    if saved and scope(primary).startswith('hub-service/'):
        models = available_models(data_dir, device_id, db, task['project_id']) if data_dir else []
        if not any(scope(item) == scope(primary) for item in models):
            # A new grant after a confirmed rejection must not revive a
            # removed service or use credentials from its obsolete revision.
            retry_at = max(retry_at, now + 600)
    if retry_at <= now:
        return primary, primary, 0
    reserve = reserve_policy(db, task['assigned_agent_id'])
    if not reserve or task.get('execution_route') != 'chat' or not data_dir:
        return primary, None, retry_at
    if models is None:
        models = available_models(data_dir, device_id, db, task['project_id'])
    match = next((m for m in models if m['model'] == reserve['model']
                  and m.get('service_revision') == reserve['service_revision'] and m.get('supports_tools')), None)
    if not match:
        return primary, None, retry_at
    alternate = validate_choice({'model': match['model'], 'reasoning_effort': 'none',
                                 'reason': '主模型额度不足，使用用户指定的总控备用服务。'}, models)
    if scope(alternate) == scope(primary):
        return primary, None, retry_at
    other_retry = available_at(db, device_id, alternate, now, capabilities)
    if other_retry > now:
        return primary, None, min(retry_at, other_retry)
    if not match['supports_images']:
        from .revision_inputs import task_input_manifest
        inputs = task_input_manifest(repo, task)['attachments']
        if any(item['content_type'].startswith('image/') for item in inputs):
            return primary, None, retry_at
    return primary, {**alternate, 'source':'quota_fallback', 'policy_revision':primary.get('policy_revision', 0)}, 0


def gate_acquisition(store, task):
    """Run inside the acquire transaction, before granting a new execution."""
    if not controller(store.db, task):
        return
    from .device_capabilities import load_device_capabilities
    device_id = task['assigned_device_id']
    caps = load_device_capabilities(store.data_dir, [device_id]).get(device_id) if store.data_dir else {}
    primary, usable, retry_at = selection(store.repo, task, store.data_dir, store.clock(), caps)
    if usable is None:
        remember_primary(store.db, task['id'], primary)
        if task.get('result_summary') != WAIT_MESSAGE:
            store.repo.update_task_status(task['id'], task['status'], WAIT_MESSAGE, execution_fenced=True)
        return {'execution': None, 'capacity_deferred': True, 'retry_at': retry_at, 'task': store.repo.get_task(task['id'])}
    current = task_choice(store.db, task['id']) or {}
    if usable != current:
        remember_primary(store.db, task['id'], primary)
        store.db.execute('UPDATE hub_task_model_choices SET selection_json=? WHERE task_id=?',
                         (json.dumps(usable, ensure_ascii=False), task['id']))


def runnable_inbox(repo, device_id, data_dir, tasks, now):
    # Most inboxes have no deferred work. Keep this path constant-cost for them.
    held = {r[0] for r in repo.connection.execute('''SELECT w.task_id FROM hub_controller_capacity_tasks w
        JOIN tasks t ON t.id=w.task_id WHERE t.assigned_device_id=? AND t.status='open' ''', (device_id,))}
    if not held:
        return tasks
    from .device_capabilities import load_device_capabilities
    caps = load_device_capabilities(data_dir, [device_id]).get(device_id)
    return [task for task in tasks if task['id'] not in held or selection(repo, task, data_dir, now, caps)[1] is not None]


def note_new_messages(repo, task_ids, data_dir):
    """Mark a saved user turn during the dispatch transaction, even with the worker offline."""
    import time
    from .model_policy import freeze_choice, select_model
    db, now = repo.connection, time.time()
    for tid in task_ids:
        row = db.execute('''SELECT a.* FROM tasks t JOIN hub_agents a ON a.id=t.assigned_agent_id
            WHERE t.id=? AND a.kind='controller' AND EXISTS (SELECT 1 FROM hub_controller_cooldowns c
            WHERE c.device_id=t.assigned_device_id AND c.retry_at>?)''', (tid, now)).fetchone()
        if not row:
            continue
        task = repo.get_task(tid)
        if task_choice(db, tid) is None:
            try:
                primary = select_model(db, {**dict(row),'device_id':task['assigned_device_id']}, data_dir, task=task)
            except ValueError:
                # Persist the message even if a separately configured primary has been removed.
                continue
            freeze_choice(db, tid, primary)
        primary, usable, _ = selection(repo, task, data_dir, now)
        if usable == primary:
            continue
        remember_primary(db, tid, primary)
        db.execute('UPDATE tasks SET result_summary=? WHERE id=?',
                   (RESERVE_MESSAGE if usable else WAIT_MESSAGE, tid))


def may_defer(db, task, record, proof, token_usage):
    if not proof or not controller(db, task):
        return False
    choice = task_choice(db, task['id']) or {}
    if (proof['schema_version'] == 2) != scope(choice).startswith('hub-service/'):
        return False
    if isinstance(token_usage, dict) and token_usage.get('output_tokens') != 0:
        return False
    # A contradictory server receipt takes precedence over a client attestation.
    effect = db.execute('''SELECT
        EXISTS(SELECT 1 FROM hub_execution_progress WHERE execution_id=? AND assistant_text!='')
        OR EXISTS(SELECT 1 FROM hub_task_artifacts WHERE task_id=? AND generation=?)
        OR EXISTS(SELECT 1 FROM hub_task_handoffs WHERE task_id=? AND generation=?)
        OR EXISTS(SELECT 1 FROM hub_agent_dispatches WHERE project_id=? AND request_key GLOB ?)
        OR EXISTS(SELECT 1 FROM hub_task_amendments WHERE source_task_id=?)
        OR EXISTS(SELECT 1 FROM hub_execution_usage WHERE execution_id=? AND output_tokens>0)''',
        (record['id'], task['id'], record['generation'], task['id'], record['generation'], task['project_id'],
         f"delegate:{task['id']}:*", task['id'], record['id'])).fetchone()[0]
    return not effect


def defer_rejection(store, task, record, digest, proof):
    now = store.clock()
    choice = task_choice(store.db, task['id']) or {}
    remember_primary(store.db, task['id'], choice)
    pair = (record['device_id'], scope(choice))
    row = store.db.execute('SELECT * FROM hub_controller_cooldowns WHERE device_id=? AND provider_scope=?', pair).fetchone()
    count = min(4, row['failures'] + 1) if row and now - row['rejected_at'] < 86400 else 1
    retry_at = now + min(3600, 600 * 2**(count - 1))
    store.db.execute('''INSERT INTO hub_controller_cooldowns VALUES (?,?,?,?,?)
        ON CONFLICT(device_id,provider_scope) DO UPDATE SET rejected_at=excluded.rejected_at,
        retry_at=excluded.retry_at,failures=excluded.failures''', (*pair, now, retry_at, count))
    primary, next_choice, _ = selection(store.repo, task, store.data_dir, now)
    summary = WAIT_MESSAGE if next_choice is None else RETRY_MESSAGE if next_choice == primary else RESERVE_MESSAGE
    updated = store.repo.update_task_status(task['id'], 'open', summary, execution_fenced=True)
    store.db.execute('INSERT INTO hub_execution_capacity_rejections VALUES (?,?)', (record['id'], proof['code']))
    store.db.execute("""UPDATE hub_task_executions SET state='yielded',completed_at=?,result_digest=?,task_version=?,
        detail=? WHERE id=?""", (now, digest, updated['status_version'], summary, record['id']))


def successful_run(db, task, record):
    if controller(db, task):
        choice = task_choice(db, task['id']) or {}
        db.execute('DELETE FROM hub_controller_cooldowns WHERE device_id=? AND provider_scope=? AND rejected_at<=?',
                   (record['device_id'], scope(choice), record['started_at']))
