"""Durable task amendments and browser-addressable execution artifacts."""
from __future__ import annotations
import hashlib
import json
import re
from urllib.parse import unquote
from .agents import now
from .execution_leases import ExecutionConflict, ExecutionStore, latest_execution, require_execution_fence


def initialize_delivery_schema(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS hub_task_artifacts (
        task_id INTEGER NOT NULL REFERENCES tasks(id), generation INTEGER NOT NULL,
        artifact_key TEXT NOT NULL, local_reference TEXT NOT NULL, attachment_id INTEGER NOT NULL REFERENCES attachments(id),
        PRIMARY KEY(task_id,generation,artifact_key));
      CREATE TABLE IF NOT EXISTS hub_task_amendments (
        id INTEGER PRIMARY KEY, source_task_id INTEGER NOT NULL REFERENCES tasks(id),
        target_task_id INTEGER NOT NULL REFERENCES tasks(id), next_task_id INTEGER NOT NULL REFERENCES tasks(id),
        request_key TEXT NOT NULL, payload_hash TEXT NOT NULL, instruction TEXT NOT NULL, mode TEXT NOT NULL,
        created_at TEXT NOT NULL, UNIQUE(source_task_id,request_key));
      CREATE TABLE IF NOT EXISTS hub_task_amendment_requests (
        source_task_id INTEGER NOT NULL REFERENCES tasks(id), request_key TEXT NOT NULL,
        amendment_id INTEGER NOT NULL REFERENCES hub_task_amendments(id),
        PRIMARY KEY(source_task_id,request_key));
    ''')
    db.commit()


def repair_user_messages(repo, project_id):
    """Backfill only proven web dispatches, preserving their historical timestamps."""
    db=repo.connection
    repaired=[]
    with db:
        db.execute('BEGIN IMMEDIATE')
        for dispatch in db.execute('SELECT * FROM hub_agent_dispatches WHERE project_id=? AND request_key LIKE \'web:%\' ORDER BY id',(project_id,)).fetchall():
            tasks=db.execute('SELECT t.* FROM tasks t JOIN hub_dispatch_tasks dt ON dt.task_id=t.id WHERE dt.dispatch_id=? ORDER BY t.id',(dispatch['id'],)).fetchall()
            if not tasks or not tasks[0]['created_by_user_id']:
                continue
            if db.execute('SELECT 1 FROM entries WHERE task_id=? AND author_user_id=? AND deleted_at IS NULL',(tasks[0]['id'],tasks[0]['created_by_user_id'])).fetchone():
                continue
            entry=repo.create_entry(project_id,'你','message',tasks[0]['description'],
                author_user_id=tasks[0]['created_by_user_id'],task_id=tasks[0]['id'],commit=False,
                metadata_json=json.dumps({'dispatch_id':dispatch['id'],'task_ids':[t['id'] for t in tasks],'recovered_user_message':True}))
            db.execute('UPDATE entries SET created_at=?,updated_at=? WHERE id=?',(dispatch['created_at'],dispatch['created_at'],entry['id']))
            repaired.append(entry['id'])
    return repaired


def artifact_links(db, task_id, generation, body, url_prefix):
    records=db.execute('SELECT ar.local_reference,a.id,a.original_filename FROM hub_task_artifacts ar JOIN attachments a ON a.id=ar.attachment_id WHERE ar.task_id=? AND ar.generation=?',(task_id,generation)).fetchall()
    aliases={}
    for row in records:
        ref=row['local_reference'].replace('\\','/')
        from pathlib import PurePosixPath
        text_preview = PurePosixPath(row['original_filename']).suffix.lower() in {'.md','.txt','.csv','.json','.log'}
        suffix=PurePosixPath(row['original_filename']).suffix.lower()
        url=f"{url_prefix}/{row['id']}" + ('/preview' if text_preview or suffix=='.pdf' else '?inline=true' if suffix in {'.png','.jpg','.jpeg','.webp','.gif','.avif','.mp4'} else '')
        aliases[ref]=(url,f"{url_prefix}/{row['id']}")
        aliases['/'+ref.lstrip('/')]=(url,f"{url_prefix}/{row['id']}")
    def replace(match):
        ref=unquote(match.group(2).strip('<>')).replace('\\','/')
        options=aliases.get(ref)
        download=bool(re.search(r'下载|download|保存(?:原图|文件)',match.group(1),re.I))
        target=options[1 if download else 0] if options else match.group(2)
        return '['+match.group(1)+']('+target+')'
    return re.sub(r'\[([^\]]+)\]\(([^\n)]+)\)',replace,body)


def revise_task(repo, source, device_id, payload):
    db=repo.connection
    target_id=payload.get('target_task_id')
    instruction=payload.get('instruction')
    key=payload.get('request_key')
    if type(target_id) is not int or not isinstance(instruction,str) or not 1<=len(instruction.strip())<=4000 or not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{8,100}',key):
        raise ValueError('修改需提供真实任务编号、具体要求和稳定请求编号')
    digest=hashlib.sha256(json.dumps([target_id,instruction],ensure_ascii=False).encode()).hexdigest()
    with db:
        db.execute('BEGIN IMMEDIATE')
        source=repo.get_task(source['id'])
        require_execution_fence(db,source,device_id,payload.get('execution'),allow_unleased=False)
        agent=db.execute('SELECT kind FROM hub_agents WHERE id=?',(source['assigned_agent_id'],)).fetchone()
        if not agent or agent['kind']!='controller':
            raise ExecutionConflict('只有当前运行的总控可以调整工作')
        previous=db.execute('''SELECT a.* FROM hub_task_amendment_requests r
            JOIN hub_task_amendments a ON a.id=r.amendment_id
            WHERE r.source_task_id=? AND r.request_key=?''',(source['id'],key)).fetchone()
        if not previous:
            # Existing records predate the request-alias table.
            previous=db.execute('SELECT * FROM hub_task_amendments WHERE source_task_id=? AND request_key=?',(source['id'],key)).fetchone()
        if previous:
            if previous['payload_hash']!=digest: raise ValueError('同一请求编号不能对应不同修改')
            return dict(previous)
        # A controller may retry the same tool call with a new request key.
        # Only collapse identical target/content within this source turn;
        # another user turn can intentionally request the same work again.
        previous=db.execute('SELECT * FROM hub_task_amendments WHERE source_task_id=? AND payload_hash=? ORDER BY id LIMIT 1',
                            (source['id'],digest)).fetchone()
        if previous:
            db.execute('INSERT INTO hub_task_amendment_requests VALUES (?,?,?)',(source['id'],key,previous['id']))
            return dict(previous)
        target=repo.get_task(target_id)
        if not target or target['project_id']!=source['project_id'] or target['conversation_id']!=source['conversation_id'] or target_id==source['id']:
            raise ValueError('只能调整当前对话内明确的已有工作')
        target_agent=db.execute('SELECT kind FROM hub_agents WHERE id=?',(target['assigned_agent_id'],)).fetchone()
        if not target_agent or target_agent['kind']!='worker': raise ValueError('请选择实际执行者的工作任务')
        # Users often refer to the original task. Follow its continuation chain
        # before appending another change, otherwise two revisions can run at once.
        visited = {target_id}
        while True:
            follow = db.execute('SELECT next_task_id FROM hub_task_amendments WHERE target_task_id=? AND next_task_id!=? ORDER BY id DESC LIMIT 1',
                                (target['id'],target['id'])).fetchone()
            if not follow or follow['next_task_id'] in visited:
                break
            candidate = repo.get_task(follow['next_task_id'])
            if not candidate or candidate['project_id'] != source['project_id'] or candidate['conversation_id'] != source['conversation_id']:
                raise ValueError('修改接续记录不一致，请先核对任务链')
            target = candidate
            visited.add(target['id'])
        target_id = target['id']
        next_task=target
        # Coalesce changes into a pending continuation; never edit a running prompt.
        pending=db.execute("SELECT id FROM tasks WHERE wait_for_task_id=? AND status='open' AND NOT EXISTS (SELECT 1 FROM hub_task_executions e WHERE e.task_id=tasks.id) ORDER BY id DESC LIMIT 1",(target_id,)).fetchone()
        if pending: next_task=repo.get_task(pending['id'])
        if next_task['status']=='open' and latest_execution(db,next_task['id']) is None:
            mode='updated'
            description=(next_task['description']+'\n\n最新用户修改（以此为准）：\n'+instruction)
            if len(description)>16000: raise ValueError('修改记录过长，请先由总控整理完整要求')
            db.execute('UPDATE tasks SET description=?,updated_at=? WHERE id=?',(description,now(),next_task['id']))
            db.execute('DELETE FROM hub_task_model_choices WHERE task_id=?',(next_task['id'],))
        else:
            mode='queued'
            description=('接续原工作 #'+str(target_id)+'。先检查原工作成果；只实施本次修改，避免重复副作用。\n原要求：\n'
                         +target['description']+'\n\n最新用户修改：\n'+instruction)
            if len(description)>16000: raise ValueError('修改记录过长，请先由总控整理完整要求')
            next_task=repo.create_task(source['project_id'],('调整：'+target['title'])[:200],
                description=description,
                assigned_agent_id=target['assigned_agent_id'],created_by_device_id=device_id,commit=False)
            db.execute('UPDATE tasks SET conversation_id=?,wait_for_task_id=?,route_engine=?,route_reason=? WHERE id=?',
                (source['conversation_id'],target_id,'controller_revision','收到修改；原执行结束并核对结果后接续',next_task['id']))
            db.execute('UPDATE tasks SET execution_mode=?,resource_keys_json=?,execution_route=?,execution_route_reason=? WHERE id=?',
                       (target.get('execution_mode','shared'),target.get('resource_keys_json','[]'),
                        target.get('execution_route','direct'),target.get('execution_route_reason',''),next_task['id']))
            repo.bind_task_attachments(source['project_id'],[next_task['id']],
                [r['attachment_id'] for r in db.execute('SELECT attachment_id FROM hub_task_attachments WHERE task_id=?',(target_id,))],source_task_id=target_id)
        repo.bind_task_attachments(source['project_id'],[next_task['id']],
            [r['attachment_id'] for r in db.execute('SELECT attachment_id FROM hub_task_attachments WHERE task_id=? AND attachment_id NOT IN (SELECT attachment_id FROM hub_task_attachments WHERE task_id=?)',(source['id'],next_task['id']))],source_task_id=source['id'])
        row_id=db.execute('INSERT INTO hub_task_amendments(source_task_id,target_task_id,next_task_id,request_key,payload_hash,instruction,mode,created_at) VALUES (?,?,?,?,?,?,?,?)',
            (source['id'],target_id,next_task['id'],key,digest,instruction,mode,now())).lastrowid
        from .revision_inputs import refresh_revision_inputs
        refresh_revision_inputs(db,repo.get_task(next_task['id']))
        db.execute('UPDATE hub_work_assignments SET task_id=? WHERE task_id=?', (next_task['id'],target_id))
        return dict(db.execute('SELECT * FROM hub_task_amendments WHERE id=?',(row_id,)).fetchone())


def dependency_ready(db, task):
    dependency=task.get('wait_for_task_id')
    if not dependency: return True
    parent=db.execute('SELECT status FROM tasks WHERE id=?',(dependency,)).fetchone()
    execution=latest_execution(db,dependency)
    return bool(parent and parent['status'] in {'review','done','blocked','cancelled'} and
                (not execution or execution['state'] in {'completed','stopped'}))


def inherit_dependency_artifacts(db, task):
    """Resolve outputs at execution time: a queued revision may predate them."""
    from .revision_inputs import refresh_revision_inputs
    refresh_revision_inputs(db,task)
