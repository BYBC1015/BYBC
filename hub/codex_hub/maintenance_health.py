"""Bounded, content-free health observations. Scanning never invokes a model."""
from __future__ import annotations

import json
import math
from collections import defaultdict

WINDOW_SECONDS = 24 * 3600
MIN_REPEATS = 3
SLOW_PUBLIC_SECONDS = 120
MAX_EXECUTIONS = 200
MAX_SIGNALS = 8


def initialize_health_schema(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS hub_maintenance_health_observations (
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        execution_id INTEGER NOT NULL REFERENCES hub_task_executions(id) ON DELETE CASCADE,
        kind TEXT NOT NULL, cause_key TEXT NOT NULL, evidence_json TEXT NOT NULL,
        observed_at REAL NOT NULL, run_id INTEGER REFERENCES hub_maintenance_runs(id),
        PRIMARY KEY(project_id,execution_id,kind));
      CREATE INDEX IF NOT EXISTS hub_maintenance_health_pending
        ON hub_maintenance_health_observations(project_id,run_id,cause_key);
      CREATE TABLE IF NOT EXISTS hub_maintenance_health_daily (
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        day_key TEXT NOT NULL, run_id INTEGER NOT NULL REFERENCES hub_maintenance_runs(id),
        PRIMARY KEY(project_id,day_key));
    ''')


def _finite(value):
    return isinstance(value, (float, int)) and math.isfinite(value)


def observe_health(db, project_id, *, now):
    """Retain qualifying evidence only; an old/partial sample cannot imply slowness.

    A blocked pattern is a diagnostic category, not an inferred root cause.
    Only current, fenced outcomes count; mutable task status cannot relabel an
    older generation. Maintenance conversations (including their summaries and
    handoffs) are excluded to keep diagnosis from diagnosing itself.
    """
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'hub_execution_timings', 'hub_task_executions'}.issubset(tables):
        return 0
    from .execution_telemetry import execution_latency
    rows = execution_latency(db, project_id, limit=MAX_EXECUTIONS)
    excluded_tasks, excluded_rooms = set(), set()
    for r in db.execute('''SELECT t.id,t.conversation_id FROM tasks t
        WHERE t.project_id=? AND (t.id IN (SELECT task_id FROM hub_maintenance_runs WHERE project_id=?)
          OR t.id IN (SELECT dt.task_id FROM hub_dispatch_tasks dt JOIN hub_agent_dispatches d ON d.id=dt.dispatch_id
                     WHERE d.project_id=? AND d.request_key LIKE 'maintenance:%'))''',
        (project_id, project_id, project_id)):
        excluded_tasks.add(r['id'])
        if r['conversation_id'] is not None:
            excluded_rooms.add(r['conversation_id'])
    metadata = {r['execution_id']: dict(r) for r in db.execute('''SELECT r.id AS execution_id,
        r.task_version,t.status_version,t.status,t.execution_route,t.conversation_id,a.kind AS agent_kind
        FROM hub_task_executions r JOIN tasks t ON t.id=r.task_id
        LEFT JOIN hub_agents a ON a.id=t.assigned_agent_id
        WHERE t.project_id=? ORDER BY r.id DESC LIMIT ?''', (project_id, MAX_EXECUTIONS))}
    oldest_id = min((row['execution_id'] for row in rows), default=0)
    assigned = {(r['execution_id'], r['kind']) for r in db.execute('''SELECT execution_id,kind
        FROM hub_maintenance_health_observations WHERE project_id=? AND run_id IS NOT NULL
        AND execution_id>=?''', (project_id,oldest_id))}
    automated = set()
    for table, column in [('hub_completion_batches', 'controller_task_id'), ('hub_task_handoffs', 'controller_task_id')]:
        if table in tables:
            automated.update(r[0] for r in db.execute(f'''SELECT x.{column} FROM {table} x
                JOIN tasks t ON t.id=x.{column} WHERE t.project_id=?''', (project_id,)))
    blocked = defaultdict(list)
    slow = []
    slow_sequence_open = True
    for row in rows:
        meta = metadata[row['execution_id']]
        if row['task_id'] in excluded_tasks or meta['conversation_id'] in excluded_rooms:
            continue
        event_at = row['completed_at'] or row['started_at'] or row['acquired_at']
        recent = _finite(event_at) and 0 <= now - event_at <= WINDOW_SECONDS
        route = meta['execution_route'] if meta['execution_route'] in {'chat', 'direct', 'project'} else 'unknown'
        evidence = {k: row[k] for k in ('execution_id', 'task_id', 'generation', 'execution_device_id')}
        evidence.update(observed_event_at=event_at, execution_route=route)
        if (recent and meta['status'] == 'blocked' and meta['task_version'] == meta['status_version']
                and row['state'] in {'completed', 'uncertain'}
                and (row['execution_id'], 'repeated_blocked') not in assigned):
            cause = f"repeated_blocked:{row['execution_device_id']}:{route}:{row['state']}"
            blocked[cause].append({**evidence, 'execution_state': row['state']})
        if meta['agent_kind'] != 'controller' or row['task_id'] in automated:
            continue
        delay = row['start_to_first_public_seconds']
        public_at = row['first_public_at'] if row['first_public_source']=='progress' else row['completed_at']
        valid_slow = (recent and row['timing_coverage'] == 'complete' and _finite(delay)
                      and _finite(public_at) and public_at <= now
                      and delay > SLOW_PUBLIC_SECONDS
                      and (row['execution_id'], 'slow_first_public') not in assigned)
        if slow_sequence_open and valid_slow:
            slow.append({**evidence, 'start_to_first_public_seconds': delay,
                         'first_public_source': row['first_public_source']})
        else:
            # Fast, still unknown, partial, or already diagnosed interrupts the
            # consecutive sequence; never cherry-pick three slow historical runs.
            slow_sequence_open = False
    groups = [('repeated_blocked', key, values) for key, values in blocked.items()
              if len({v['task_id'] for v in values}) >= MIN_REPEATS]
    if len({v['task_id'] for v in slow}) >= MIN_REPEATS:
        groups.append(('slow_first_public', 'slow_first_public', slow))
    inserted = 0
    for kind, cause, values in groups:
        for evidence in values:
            inserted += db.execute('''INSERT OR IGNORE INTO hub_maintenance_health_observations
                (project_id,execution_id,kind,cause_key,evidence_json,observed_at) VALUES (?,?,?,?,?,?)''',
                (project_id, evidence['execution_id'], kind, cause,
                 json.dumps(evidence, separators=(',', ':')), now)).rowcount
    return inserted


def pending_signals(db, project_id, *, now):
    """Bound evidence size; same-category results awaiting verification do not
    launch another diagnosis. Other categories may proceed after a review run.
    """
    rows = db.execute('''SELECT cause_key,kind,COUNT(*) AS sample_count,MAX(observed_at) AS latest
        FROM hub_maintenance_health_observations WHERE project_id=? AND run_id IS NULL
        AND observed_at>=? GROUP BY cause_key,kind HAVING COUNT(*)>=?
        ORDER BY latest,cause_key LIMIT ?''', (project_id, now-WINDOW_SECONDS, MIN_REPEATS, MAX_SIGNALS)).fetchall()
    result = []
    for row in rows:
        awaiting = db.execute('''SELECT r.task_id FROM hub_maintenance_health_observations h
            JOIN hub_maintenance_runs r ON r.id=h.run_id JOIN tasks t ON t.id=r.task_id
            WHERE h.project_id=? AND h.cause_key=? AND t.status IN ('review','blocked') LIMIT 1''',
            (project_id, row['cause_key'])).fetchone()
        evidence = [json.loads(r['evidence_json']) for r in db.execute('''SELECT evidence_json
            FROM hub_maintenance_health_observations WHERE project_id=? AND cause_key=? AND run_id IS NULL
            AND observed_at>=? ORDER BY execution_id DESC LIMIT 8''', (project_id,row['cause_key'],now-WINDOW_SECONDS))]
        result.append({**dict(row), 'evidence': evidence,
                       'awaiting_verification_task_id': awaiting['task_id'] if awaiting else None})
    return result


def link_signals(db, project_id, run_id, signals):
    for signal in signals:
        db.execute('''UPDATE hub_maintenance_health_observations SET run_id=?
            WHERE project_id=? AND cause_key=? AND run_id IS NULL''', (run_id,project_id,signal['cause_key']))
