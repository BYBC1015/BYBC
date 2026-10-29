"""Bounded public execution drafts and content-free workspace notifications."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import quote

from .execution_leases import ExecutionConflict

ASSISTANT_LIMIT = 24000
STATUS_LIMIT = 300
SSE_MAX_SECONDS = 55
SSE_POLL_SECONDS = 1
SSE_HEARTBEAT_SECONDS = 15
ACTIVITY_CLOCK_SECONDS = 15


def initialize_progress_schema(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS hub_execution_progress (
        execution_id INTEGER PRIMARY KEY REFERENCES hub_task_executions(id) ON DELETE CASCADE,
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        sequence INTEGER NOT NULL, last_kind TEXT NOT NULL, last_digest TEXT NOT NULL,
        status_text TEXT NOT NULL DEFAULT '', assistant_text TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL
      );
      CREATE INDEX IF NOT EXISTS hub_execution_progress_task ON hub_execution_progress(task_id);
      CREATE TABLE IF NOT EXISTS hub_project_event_versions (
        project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
        version INTEGER NOT NULL DEFAULT 0
      );
    ''')
    from .execution_telemetry import initialize_execution_telemetry
    initialize_execution_telemetry(db)
    # Registration runs again after all additive schemas exist. Triggers observe
    # changes by every process, including completion and manual status updates.
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    project_tables = ['tasks','entries','attachments','hub_agents','hub_conversations','project_memberships']
    linked_tables = ['hub_task_executions','hub_execution_progress','hub_task_handoffs','hub_task_model_choices']
    for table in project_tables + linked_tables:
        if table not in tables:
            continue
        for operation, row in [('INSERT','NEW'),('UPDATE','NEW'),('DELETE','OLD')]:
            project = f'{row}.project_id' if table in project_tables else f'(SELECT project_id FROM tasks WHERE id={row}.task_id)'
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS hub_events_{table}_{operation.lower()}
                AFTER {operation} ON {table} BEGIN
                  INSERT INTO hub_project_event_versions(project_id,version)
                    SELECT id,1 FROM projects WHERE id={project}
                    ON CONFLICT(project_id) DO UPDATE SET version=version+1;
                END''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS hub_events_devices_update AFTER UPDATE ON devices
      WHEN OLD.last_seen_at IS NOT NEW.last_seen_at OR OLD.last_web_seen_at IS NOT NEW.last_web_seen_at
        OR OLD.last_agent_seen_at IS NOT NEW.last_agent_seen_at OR OLD.label IS NOT NEW.label
        OR OLD.is_active IS NOT NEW.is_active OR OLD.server_ai_provider IS NOT NEW.server_ai_provider
        OR OLD.note IS NOT NEW.note BEGIN
        INSERT INTO hub_project_event_versions(project_id,version)
          SELECT p.id,1 FROM projects p JOIN project_memberships m ON m.project_id=p.id
          WHERE m.device_id=NEW.id
          ON CONFLICT(project_id) DO UPDATE SET version=version+1;
      END''')
    db.commit()


def write_progress(store, task_id, device_id, data):
    allowed = {'generation','run_key','sequence','kind','text'}
    if set(data) - allowed:
        raise ValueError('进度仅允许公开状态或公开答复，不能包含推理过程和原始工具数据。')
    sequence, kind, text = data.get('sequence'), data.get('kind'), data.get('text')
    if type(sequence) is not int or not 0 < sequence <= 2_147_483_647:
        raise ValueError('进度 sequence 必须为递增正整数。')
    if kind not in {'status','assistant'} or not isinstance(text,str):
        raise ValueError('进度仅支持 status 或 assistant 文本。')
    if len(text) > (STATUS_LIMIT if kind=='status' else ASSISTANT_LIMIT):
        raise ValueError('公开进度超过长度限制。')
    # The bridge extracts public assistant messages; this endpoint never accepts
    # private reasoning/tool event envelopes under an alternative field or kind.
    if any(marker in text.casefold() for marker in ('<think>','<analysis>','<hub-reflection>','<hub-artifacts>')):
        raise ValueError('内部推理或交付协议不能作为公开进度。')
    digest=hashlib.sha256(json.dumps([kind,text],ensure_ascii=False).encode()).hexdigest()
    with store.transaction():
        task,run=store._fenced(task_id,device_id,data.get('generation'),data.get('run_key'))
        if (run['state']!='running' or run['lease_until']<=store.clock()
                or task['status']!='in_progress' or run['task_version']!=task['status_version']):
            raise ExecutionConflict('任务已结束、发生改变或执行凭据已失效，旧进度不能覆盖当前工作。')
        prior=store.db.execute('SELECT * FROM hub_execution_progress WHERE execution_id=?',(run['id'],)).fetchone()
        if prior:
            if sequence<prior['sequence']:
                return {'accepted':False,'ignored':True,'sequence':prior['sequence'],'generation':run['generation']}
            if sequence==prior['sequence']:
                if digest!=prior['last_digest']:
                    raise ExecutionConflict('相同进度序号不能写入不同内容。')
                return {'accepted':True,'replayed':True,'sequence':sequence,'generation':run['generation']}
            if kind=='assistant' and not text.startswith(prior['assistant_text']):
                raise ExecutionConflict('公开答复进度只能在原草稿后追加，不能回退或改写已展示内容。')
        status_text=text if kind=='status' else prior['status_text'] if prior else ''
        assistant_text=text if kind=='assistant' else prior['assistant_text'] if prior else ''
        store.db.execute('''INSERT INTO hub_execution_progress
          (execution_id,task_id,sequence,last_kind,last_digest,status_text,assistant_text,updated_at)
          VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(execution_id) DO UPDATE SET
          sequence=excluded.sequence,last_kind=excluded.last_kind,last_digest=excluded.last_digest,
          status_text=excluded.status_text,assistant_text=excluded.assistant_text,updated_at=excluded.updated_at''',
          (run['id'],task_id,sequence,kind,digest,status_text,assistant_text,store.clock()))
    return {'accepted':True,'replayed':False,'sequence':sequence,'generation':run['generation']}


def clear_progress(db,task_id):
    db.execute('DELETE FROM hub_execution_progress WHERE task_id=?',(task_id,))


def _duration(seconds):
    seconds=max(0,int(seconds))
    if seconds<60:return f'{seconds} 秒'
    if seconds<3600:return f'{seconds//60} 分 {seconds%60} 秒'
    return f'{seconds//3600} 小时 {(seconds%3600)//60} 分'


def annotate_progress_timing(row, at):
    """Age actual reports; neither heartbeats nor time imply new tool progress."""
    from .main import (parse_iso_datetime, AGENT_WORKER_ONLINE_WINDOW_SECONDS,
                       DEVICE_ONLINE_WINDOW_SECONDS)
    sampled=int(at//ACTIVITY_CLOCK_SECONDS)*ACTIVITY_CLOCK_SECONDS
    row['timing_bucket']=int(at//ACTIVITY_CLOCK_SECONDS)
    # Let the browser update contact/age independently of Markdown and media.
    row['public_text_version']=hashlib.sha256(row['assistant_text'].encode('utf-8')).hexdigest()[:16]
    row['run_age_label']=_duration(sampled-row['started_at']) if row.get('started_at') is not None else '尚未确认'
    row['has_public_report']=bool(row['has_public_report'])
    row['report_age_label']=_duration(sampled-row['updated_at']) if row['updated_at'] is not None else '尚未收到'
    row['execution_contact_valid']=row['execution_state']=='running' and row['lease_until']>at
    seen=parse_iso_datetime(str(row.get('last_agent_seen_at') or ''))
    window=AGENT_WORKER_ONLINE_WINDOW_SECONDS if row['agent_tasks_only'] else DEVICE_ONLINE_WINDOW_SECONDS
    row['device_contact_recent']=bool(seen and 0<=at-seen.timestamp()<=window)
    row['device_contact_age_label']=_duration(sampled-seen.timestamp()) if seen else '尚未收到'
    return row


def active_progress(db,project_id,*,conversation_id=None,task_ids=None,limit=12,clock=time.time,include_expired=False):
    at=clock()
    params=[project_id]
    lease_clause='' if include_expired else ' AND r.lease_until>?'
    if not include_expired:params.append(at)
    # A confirmed start is visible before its first public packet. An uncertain
    # execution retains evidence already received, but never looks live. Neither
    # read path inserts a progress row or invents report/response timestamps.
    state_clause="(r.state='running' AND t.status='in_progress')"
    if include_expired:
        state_clause="("+state_clause+" OR (r.state='uncertain' AND t.status='blocked'))"
    clause=''
    if conversation_id is not None:
        clause+=' AND t.conversation_id=?';params.append(conversation_id)
    if task_ids is not None:
        ids=list(dict.fromkeys(task_ids))[:160]
        if not ids:return []
        clause+=' AND t.id IN ('+','.join('?' for _ in ids)+')';params.extend(ids)
    params.append(max(1,min(160,limit)))
    return [annotate_progress_timing(dict(row),at) for row in db.execute('''SELECT t.id AS task_id,r.generation,
        COALESCE(p.sequence,0) AS sequence,COALESCE(p.status_text,'') AS status_text,
        COALESCE(p.assistant_text,'') AS assistant_text,COALESCE(p.last_kind,'') AS last_kind,
        p.execution_id IS NOT NULL AS has_public_report,r.state AS execution_state,
        p.updated_at,r.started_at,r.lease_until,t.assigned_agent_id,t.execution_route,
        a.name AS agent_name,a.kind AS agent_kind,d.label AS device_label,d.last_agent_seen_at,
        EXISTS(SELECT 1 FROM hub_agent_connections c WHERE c.device_id=d.id) AS agent_tasks_only
        FROM hub_task_executions r LEFT JOIN hub_execution_progress p ON p.execution_id=r.id
        JOIN tasks t ON t.id=r.task_id JOIN hub_agents a ON a.id=t.assigned_agent_id JOIN devices d ON d.id=r.device_id
        WHERE t.project_id=? '''+lease_clause+' AND '+state_clause+'''
        AND r.task_version=t.status_version AND r.device_id=t.assigned_device_id
        AND r.generation=(SELECT MAX(last.generation) FROM hub_task_executions last WHERE last.task_id=t.id)
        '''+clause+' ORDER BY r.started_at,r.id LIMIT ?',params)]


class StreamLimiter:
    """Bound connections per project, per owner and per server process."""
    def __init__(self):
        self.lock=threading.Lock();self.counts={}

    def acquire(self,user_id,project_id):
        key=(user_id,project_id)
        with self.lock:
            if (self.counts.get(key,0)>=3 or sum(v for k,v in self.counts.items() if k[0]==user_id)>=12
                    or sum(self.counts.values())>=32):
                return None
            self.counts[key]=self.counts.get(key,0)+1
        released=False
        def release():
            nonlocal released
            with self.lock:
                if released:return
                released=True
                self.counts[key]-=1
                if not self.counts[key]:del self.counts[key]
        return release


def event_snapshot(database_path,session_token,user_id,project_id):
    """One bounded read-only connection per tick; never hold a request DB or transaction."""
    from .repository import Repository
    uri='file:'+quote(str(Path(database_path).resolve()).replace('\\','/'),safe='/:')+'?mode=ro'
    db=sqlite3.connect(uri,uri=True,timeout=1)
    db.row_factory=sqlite3.Row
    try:
        repo=Repository(db)
        user=repo.get_session_user(session_token)
        if (not user or user['id']!=user_id or user.get('role')!='owner'
                or not db.execute('SELECT 1 FROM projects WHERE id=?',(project_id,)).fetchone()):
            return None
        return project_event_version(db,project_id)
    finally:
        db.close()


def project_event_version(db,project_id):
    from .repository import Repository
    from .main import annotate_membership_presence
    row=db.execute('SELECT version FROM hub_project_event_versions WHERE project_id=?',(project_id,)).fetchone()
    version=row['version'] if row else 0
    members=annotate_membership_presence(Repository(db).list_project_memberships(project_id))
    presence=[(m['device_id'],m['presence_mode']) for m in members]
    # Expired presence and expired draft leases change the view even when no
    # process can write another row to notify us of its disappearance.
    at=time.time()
    activity=db.execute('''SELECT COUNT(*) AS count,SUM(r.state='running' AND r.lease_until>?) AS live
      FROM hub_task_executions r JOIN tasks t ON t.id=r.task_id
      WHERE t.project_id=? AND r.device_id=t.assigned_device_id
      AND ((r.state='running' AND t.status='in_progress') OR (r.state='uncertain' AND t.status='blocked'))
      AND r.task_version=t.status_version
      AND r.generation=(SELECT MAX(e.generation) FROM hub_task_executions e WHERE e.task_id=t.id)''',(at,project_id)).fetchone()
    # Existing SSE/polling redraws only the age metadata. This is not a new
    # progress packet and does not write storage or invoke a model.
    activity_clock=int(at//ACTIVITY_CLOCK_SECONDS) if activity['count'] else None
    suffix=hashlib.sha256(json.dumps([presence,activity['live'] or 0,activity_clock],sort_keys=True).encode()).hexdigest()[:12]
    return f'{version}.{suffix}'


async def workspace_event_stream(request,*,database_path,session_token,user_id,project_id,release,baseline=None):
    start=time.monotonic();last_heartbeat=start;previous=baseline
    try:
        yield 'retry: 1000\n\n'
        while time.monotonic()-start<SSE_MAX_SECONDS:
            if await request.is_disconnected():break
            try:
                version=await asyncio.to_thread(event_snapshot,database_path,session_token,user_id,project_id)
            except sqlite3.Error:
                # The browser will reconnect or keep its bounded polling fallback.
                break
            if version is None:
                yield 'event: session-expired\ndata: {}\n\n'
                break
            if version!=previous:
                yield f'id: {version}\nevent: workspace\ndata: '+json.dumps({'version':version},separators=(',',':'))+'\n\n'
                previous=version
            if time.monotonic()-last_heartbeat>=SSE_HEARTBEAT_SECONDS:
                yield ': heartbeat\n\n';last_heartbeat=time.monotonic()
            await asyncio.sleep(SSE_POLL_SECONDS)
    finally:
        release()
