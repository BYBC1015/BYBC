"""Evidence-backed retirement of input failures superseded by a successful resend."""
from __future__ import annotations

import hashlib
import json
import logging
import re

from .agents import now
from .execution_leases import latest_execution


def initialize_resubmission_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS hub_task_resubmissions (
        source_task_id INTEGER PRIMARY KEY REFERENCES tasks(id),
        successor_task_id INTEGER NOT NULL REFERENCES tasks(id),
        source_status_version INTEGER NOT NULL,
        evidence_kind TEXT NOT NULL CHECK(evidence_kind IN ('user_confirmed','identical_inputs')),
        evidence_json TEXT NOT NULL, confirmed_by_user_id INTEGER REFERENCES users(id),
        reason TEXT NOT NULL, created_at TEXT NOT NULL)''')


def _successful_result(db, task):
    if not task or task['status'] not in {'review','done'} or task.get('coordination_state')=='waiting':
        return False
    execution=latest_execution(db,task['id'])
    if not execution or execution['state']!='completed' or not execution.get('result_entry_id'):
        return False
    return bool(db.execute('''SELECT 1 FROM entries WHERE id=? AND task_id=? AND project_id=?
        AND deleted_at IS NULL''',(execution['result_entry_id'],task['id'],task['project_id'])).fetchone())


def _same_unstarted_work(db, source, successor):
    return bool(source and successor and source['id']<successor['id']
        and source['status']=='blocked' and source.get('coordination_state')!='waiting'
        and source.get('conversation_id') and source.get('assigned_agent_id')
        and (source['project_id'],source['conversation_id'],source['assigned_agent_id'])==
            (successor['project_id'],successor['conversation_id'],successor['assigned_agent_id'])
        and source['description'].strip() and source['description'].strip()==successor['description'].strip()
        and not db.execute('SELECT 1 FROM hub_task_executions WHERE task_id=? LIMIT 1',(source['id'],)).fetchone()
        and _successful_result(db,successor))


def _input_fingerprints(db, task_id):
    rows=db.execute('''SELECT a.sha256,a.byte_size,e.deleted_at,e.id AS entry_id,
        a.project_id AS attachment_project,t.project_id AS task_project FROM hub_task_attachments ta
        JOIN attachments a ON a.id=ta.attachment_id JOIN tasks t ON t.id=ta.task_id
        LEFT JOIN entries e ON e.id=a.entry_id
        WHERE ta.task_id=?''',(task_id,)).fetchall()
    if not rows or any(row['attachment_project']!=row['task_project'] or not row['entry_id']
        or row['deleted_at'] is not None or row['byte_size']<=0
        or not re.fullmatch('[0-9a-fA-F]{64}',row['sha256'] or '') for row in rows):
        return []
    # A multiset retains intentionally repeated inputs; filenames are not proof.
    return sorted((row['sha256'].lower(),row['byte_size']) for row in rows)


def _input_preflight_failure(task):
    summary=task.get('result_summary') or ''
    prefix='智能体尚未开始工作：'
    return summary.startswith(prefix) and summary[len(prefix):].startswith((
        '图片像素超过', '图片解码所需内存超过', '图片无法安全解码',
        '本任务附件超过', '本任务附件总大小超过', '附件完整性校验失败',
        '附件编号、大小或校验摘要不符合限制', '任务附件准备失败：'))


def _record_and_archive(db, source, successor, *, evidence_kind, evidence, reason,
                        confirmed_by_user_id=None, archive_restored=False):
    prior=db.execute('SELECT * FROM hub_task_resubmissions WHERE source_task_id=?',(source['id'],)).fetchone()
    if prior:
        if prior['successor_task_id']!=successor['id']:
            raise ValueError('此任务已有明确重发关系，请先核对原记录')
        # Replaying registration must not undo a later explicit restore.
        return {**dict(prior),'created':False,'archived':False}
    db.execute('''INSERT INTO hub_task_resubmissions VALUES (?,?,?,?,?,?,?,?)''',
        (source['id'],successor['id'],source['status_version'],evidence_kind,
         json.dumps(evidence,ensure_ascii=False,sort_keys=True),confirmed_by_user_id,reason,now()))
    archived=source['board_state']=='auto' or (archive_restored and source['board_state']=='active')
    if archived:
        db.execute("UPDATE tasks SET board_state='archived',board_archive_reason=? WHERE id=?",
                   ('superseded:'+str(successor['id']),source['id']))
    return {**dict(db.execute('SELECT * FROM hub_task_resubmissions WHERE source_task_id=?',(source['id'],)).fetchone()),
            'created':True,'archived':archived}


def register_confirmed_resubmission(repo, source_task_id, successor_task_id, *, confirmed_by_user_id,
                                     reason, archive_restored=False):
    """Trusted maintenance call only; requires a transaction and explicit user evidence.

    Different SHA values are permitted here (for example an explicitly confirmed
    resized upload). No HTTP/model endpoint can assert this confirmation.
    """
    db=repo.connection
    if not db.in_transaction:raise RuntimeError('Resubmission registration requires a transaction')
    if type(confirmed_by_user_id) is not int or not db.execute(
            "SELECT 1 FROM users WHERE id=? AND role='owner'",(confirmed_by_user_id,)).fetchone():
        raise ValueError('需记录确实作出重发确认的用户')
    if not isinstance(reason,str) or not 1<=len(reason.strip())<=1000:
        raise ValueError('需记录用户明确确认的依据')
    source,successor=repo.get_task(source_task_id),repo.get_task(successor_task_id)
    if not _same_unstarted_work(db,source,successor):
        raise ValueError('重发关系须为同对话同智能体同完整要求、旧项从未开工、后项真实成功结束')
    return _record_and_archive(db,source,successor,evidence_kind='user_confirmed',
        evidence={'requirement_sha256':hashlib.sha256(source['description'].strip().encode()).hexdigest(),
                  'source_inputs':_input_fingerprints(db,source_task_id),
                  'successor_inputs':_input_fingerprints(db,successor_task_id)},
        reason=reason.strip(),confirmed_by_user_id=confirmed_by_user_id,archive_restored=archive_restored)


def archive_completed_resubmissions(repo, successor_task_id):
    """Bounded exact-match inference, only on result writes, never list reads."""
    db=repo.connection
    if not db.in_transaction:raise RuntimeError('Resubmission organization requires a result transaction')
    db.execute('SAVEPOINT resubmission_organization')
    try:
        archived=[]
        successor=repo.get_task(successor_task_id)
        if _successful_result(db,successor) and successor.get('conversation_id') and successor.get('created_by_user_id'):
            fingerprints=_input_fingerprints(db,successor_task_id)
            if fingerprints:
                candidates=db.execute('''SELECT * FROM tasks t WHERE project_id=? AND conversation_id=?
                    AND assigned_agent_id=? AND created_by_user_id=? AND id<? AND status='blocked' AND board_state='auto'
                    AND NOT EXISTS(SELECT 1 FROM hub_task_executions e WHERE e.task_id=t.id)
                    AND NOT EXISTS(SELECT 1 FROM hub_task_resubmissions r WHERE r.source_task_id=t.id)
                    ORDER BY id DESC LIMIT 100''',(successor['project_id'],successor['conversation_id'],
                    successor['assigned_agent_id'],successor['created_by_user_id'],successor_task_id)).fetchall()
                for row in candidates:
                    source=dict(row)
                    if (_same_unstarted_work(db,source,successor) and _input_preflight_failure(source)
                            and _input_fingerprints(db,source['id'])==fingerprints):
                        result=_record_and_archive(db,source,successor,evidence_kind='identical_inputs',
                            evidence={'requirement_sha256':hashlib.sha256(source['description'].strip().encode()).hexdigest(),
                                      'input_fingerprints':fingerprints},reason='相同完整要求及附件内容重发后已成功执行')
                        if result['archived']:archived.append(source['id'])
    except Exception as exc:
        db.execute('ROLLBACK TO resubmission_organization')
        logging.getLogger(__name__).warning('Resubmission organization skipped (%s)',type(exc).__name__)
        archived=[]
    finally:
        db.execute('RELEASE resubmission_organization')
    return archived
