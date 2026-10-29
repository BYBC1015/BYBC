"""Generation-scoped reported token counts, never estimates or price claims."""
from __future__ import annotations

import json
import re

FIELDS=('input_tokens','cached_input_tokens','output_tokens')


def initialize_execution_usage(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS hub_execution_usage (
        execution_id INTEGER PRIMARY KEY REFERENCES hub_task_executions(id) ON DELETE CASCADE,
        model_id TEXT NOT NULL DEFAULT '', reasoning_effort TEXT NOT NULL DEFAULT '',
        selection_source TEXT NOT NULL DEFAULT '', model_snapshot_known INTEGER NOT NULL DEFAULT 0,
        input_tokens INTEGER, cached_input_tokens INTEGER, output_tokens INTEGER,
        reported_at REAL, report_source TEXT NOT NULL DEFAULT '', conflicting_report INTEGER NOT NULL DEFAULT 0
    );''')


def capture_execution_model(db, execution):
    row=db.execute('SELECT selection_json FROM hub_task_model_choices WHERE task_id=?',(execution['task_id'],)).fetchone()
    try:
        choice=json.loads(row[0]) if row else {}
    except (ValueError,TypeError):
        choice={}
    if not isinstance(choice,dict):choice={}
    # Only bounded identifiers; arbitrary reasoning prose and prompts stay out.
    def identifier(key):
        value=choice.get(key)
        return value if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,100}',value) else ''
    model=identifier('model')
    db.execute('''INSERT OR IGNORE INTO hub_execution_usage
        (execution_id,model_id,reasoning_effort,selection_source,model_snapshot_known)
        VALUES (?,?,?,?,?)''',(execution['id'],model,identifier('reasoning_effort'),identifier('source'),int(bool(model))))


def normalize_usage(value):
    if not isinstance(value,dict):return None
    if any(type(value.get(key)) is not int or not 0<=value[key]<2**53 for key in FIELDS):return None
    if value['cached_input_tokens']>value['input_tokens']:return None
    return {key:value[key] for key in FIELDS}


def record_execution_usage(db, execution_id, token_usage, reported_at):
    usage=normalize_usage(token_usage)
    if usage is None:return
    # Existing pre-upgrade executions may report counts, but their former model
    # selection cannot be reconstructed from today's mutable task preferences.
    db.execute('INSERT OR IGNORE INTO hub_execution_usage(execution_id) VALUES (?)',(execution_id,))
    prior=db.execute('SELECT input_tokens,cached_input_tokens,output_tokens FROM hub_execution_usage WHERE execution_id=?',(execution_id,)).fetchone()
    if prior['input_tokens'] is not None:
        if any(prior[key]!=usage[key] for key in FIELDS):
            db.execute('UPDATE hub_execution_usage SET conflicting_report=1 WHERE execution_id=?',(execution_id,))
        return
    db.execute('''UPDATE hub_execution_usage SET input_tokens=?,cached_input_tokens=?,output_tokens=?,
        reported_at=?,report_source='codex_cli_turn_completed' WHERE execution_id=?''',
        (*(usage[key] for key in FIELDS),reported_at,execution_id))


def usage_report(db, project_id, *, limit=100):
    rows=[dict(row) for row in db.execute('''SELECT r.id AS execution_id,r.task_id,r.generation,r.state,
        r.device_id AS execution_device_id,u.model_id,u.reasoning_effort,u.selection_source,
        u.model_snapshot_known,u.input_tokens,u.cached_input_tokens,u.output_tokens,u.reported_at,
        u.report_source,u.conflicting_report FROM hub_task_executions r JOIN tasks t ON t.id=r.task_id
        LEFT JOIN hub_execution_usage u ON u.execution_id=r.id WHERE t.project_id=? ORDER BY r.id DESC LIMIT ?''',
        (project_id,max(1,min(int(limit),500))))]
    measured=[row for row in rows if row['input_tokens'] is not None and not row['conflicting_report']]
    return {'executions':rows,'observed_execution_count':len(rows),'measured_execution_count':len(measured),
        'unknown_execution_count':len(rows)-len(measured),
        'reported_totals':{key:sum(row[key] for row in measured) for key in FIELDS},
        'measurement_note':'仅统计本页执行器真实上报且无冲突的用量。缓存输入包含在输入中，不能重复相加；缓存不表示免费。未上报不等于零，未估算金额。模型字段为执行时的选择记录，不代表服务商已证明实际路由型号。'}
