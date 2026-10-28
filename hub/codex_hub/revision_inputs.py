"""Bounded revision inputs; source files and historical deliveries stay discoverable."""
from __future__ import annotations

import re
import json
import logging
from pathlib import PurePath

from .execution_leases import ExecutionConflict

MAX_FILES = 12
MAX_BYTES = 100 * 1024 * 1024
MAX_FILE_BYTES = 50 * 1024 * 1024


def _rows(db, task):
    return [dict(row) for row in db.execute('''SELECT a.id,a.entry_id,a.original_filename,a.content_type,
        a.byte_size,a.sha256,ta.task_id,ta.source_task_id,e.author_type,e.task_id AS original_task_id,
        ar.task_id AS producer_task_id,ar.generation AS artifact_generation
        FROM hub_task_attachments ta JOIN attachments a ON a.id=ta.attachment_id
        JOIN tasks t ON t.id=ta.task_id LEFT JOIN entries e ON e.id=a.entry_id
        LEFT JOIN hub_task_artifacts ar ON ar.attachment_id=a.id
        WHERE ta.task_id=? AND t.project_id=? AND a.project_id=t.project_id AND e.deleted_at IS NULL
        ORDER BY a.id''',(task['id'],task['project_id']))]


def _lineage(db, task):
    lineage=[];seen={task['id']};current=task
    while current.get('wait_for_task_id'):
        parent=db.execute('SELECT * FROM tasks WHERE id=?',(current['wait_for_task_id'],)).fetchone()
        if (not parent or parent['project_id']!=task['project_id']
                or parent['conversation_id']!=task['conversation_id']):
            raise ExecutionConflict('接续任务的原工作不属于同一项目和对话')
        if parent['id'] in seen or len(lineage)>=64:
            raise ExecutionConflict('接续任务依赖存在循环或过深，需核对')
        seen.add(parent['id']);lineage.append(parent['id']);current=dict(parent)
    return lineage


def _latest_outputs(db, task, dependency):
    return [dict(row) for row in db.execute('''SELECT a.id,a.entry_id,a.original_filename,a.content_type,
        a.byte_size,a.sha256,? AS task_id,? AS source_task_id,e.author_type,e.task_id AS original_task_id,
        ar.task_id AS producer_task_id,ar.generation AS artifact_generation
        FROM hub_task_executions r JOIN hub_task_artifacts ar ON ar.task_id=r.task_id AND ar.generation=r.generation
        JOIN attachments a ON a.id=ar.attachment_id JOIN entries e ON e.id=a.entry_id
        WHERE r.task_id=? AND r.state='completed' AND r.result_entry_id=e.id AND e.deleted_at IS NULL
        AND a.project_id=? AND r.generation=(SELECT MAX(generation) FROM hub_task_executions WHERE task_id=?)
        ORDER BY a.id''',(task['id'],dependency,dependency,task['project_id'],dependency))]


def _preview_key(name):
    stem=PurePath(name).stem.casefold()
    dimensions=re.search(r'(?:^|[-_ ])(\d{2,6})x(\d{2,6})(?=$|[-_ ])',stem)
    pixels=int(dimensions[1])*int(dimensions[2]) if dimensions else None
    preview=bool(re.search(r'(?:^|[-_ ])preview(?:$|[-_ ])',stem))
    stem=re.sub(r'(?:^|[-_ ])(?:preview|final)(?=$|[-_ ])','-',stem)
    stem=re.sub(r'(?:^|[-_ ])\d{2,6}x\d{2,6}(?=$|[-_ ])','-',stem)
    return re.sub(r'[-_ ]+','-',stem).strip('-'),pixels,preview


def _without_duplicate_previews(rows):
    originals={}
    for row in rows:
        key,pixels,preview=_preview_key(row['original_filename'])
        if not preview and pixels and row['content_type'].startswith('image/') and row['byte_size']<=MAX_FILE_BYTES:
            originals[key]=max(pixels,originals.get(key,0))
    result=[];omitted=[]
    for row in rows:
        key,pixels,preview=_preview_key(row['original_filename'])
        if preview and pixels and originals.get(key,0)>pixels:
            omitted.append({**row,'omission_reason':'同一轮已有对应完整图，小预览保留在源任务'})
        else:result.append(row)
    return result,omitted


def revision_candidates(db, task):
    bound=_rows(db,task)
    if not task.get('wait_for_task_id'):
        return bound,[]
    lineage=_lineage(db,task);dependency=lineage[0]
    required={row['id']:row for row in bound if row['producer_task_id'] not in lineage}
    archived=[{**row,'omission_reason':'历史交付保留在源任务；本轮接续最新成果'} for row in bound
              if row['producer_task_id'] in lineage]
    # Explicit ID+filename / attachment link is traceable; never guess references
    # by matching topics, titles or neighboring task IDs.
    from .conversation_context import conversation_files
    description=task.get('description','')
    for row in conversation_files(db,task['project_id'],task.get('conversation_id')):
        explicit=(f"{row['id']}-{row['original_filename']}" in description or
                  bool(re.search(r'/attachments/'+str(row['id'])+r'(?:[/?#)\s]|$)',description)))
        if explicit:
            required.setdefault(row['id'],{**row,'task_id':task['id'],'producer_task_id':None,
                'artifact_generation':None,'original_task_id':row['source_task_id'],
                'author_type':'user' if row['origin']=='user_input' else 'device'})
    outputs,previews=_without_duplicate_previews(_latest_outputs(db,task,dependency))
    output_ids={row['id'] for row in outputs}
    archived=[row for row in archived if row['id'] not in output_ids]
    archived={row['id']:row for row in archived+previews}
    # User originals remain before generated variants on every revision. The
    # newest correction remains first even when the retained source set is large.
    newest=max((row.get('original_task_id') or 0 for row in required.values() if row.get('author_type')=='user'),default=0)
    ordered=sorted([{**row,'required_input':True} for row in required.values()],key=lambda row:(
        0 if row.get('author_type')=='user' and row.get('original_task_id')==newest else 1,
        row['id']))
    return ordered+[row for row in outputs if row['id'] not in required],list(archived.values())


def task_input_manifest(repo, task):
    if not task.get('wait_for_task_id'):
        return {'attachments':repo.list_task_attachments(task['project_id'],task['id'])}
    run=repo.connection.execute('SELECT * FROM hub_task_executions WHERE task_id=? ORDER BY generation DESC LIMIT 1',(task['id'],)).fetchone()
    if run and run['state'] in {'prepared','running','uncertain'}:
        if run['input_manifest_json']:
            return json.loads(run['input_manifest_json'])
        # A pre-upgrade running execution has no snapshot. Do not reinterpret
        # or change the files it already started with.
        return {'attachments':repo.list_task_attachments(task['project_id'],task['id'])}
    rows,archived=revision_candidates(repo.connection,task)
    selected=[];omitted=list(archived);size=0
    for row in rows:
        if len(selected)>=MAX_FILES or row['byte_size']>MAX_FILE_BYTES or size+row['byte_size']>MAX_BYTES:
            omitted.append({**row,'omission_reason':'本轮预载容量不足，仍可从资料目录分批补读',
                            'required_unloaded':row.get('required_input',False)})
        else:selected.append(row);size+=row['byte_size']
    # A small derivative remains useful if its full-size counterpart did not fit
    # the download budget. Never load both just to fill the remaining capacity.
    selected_keys={_preview_key(row['original_filename'])[0] for row in selected}
    for row in archived:
        key,_,preview=_preview_key(row['original_filename'])
        if (preview and row['omission_reason'].startswith('同一轮') and key not in selected_keys
                and len(selected)<MAX_FILES and row['byte_size']<=MAX_FILE_BYTES and size+row['byte_size']<=MAX_BYTES):
            selected.append(row);size+=row['byte_size'];selected_keys.add(key)
    selected_ids={row['id'] for row in selected}
    omitted={row['id']:row for row in omitted if row['id'] not in selected_ids}
    references=[{'id':row['id'],'original_filename':row['original_filename'],
        'source_task_id':row.get('source_task_id'),'byte_size':row['byte_size'],
        'reason':row['omission_reason'],'required_unloaded':row.get('required_unloaded',False)} for row in omitted.values()]
    return {'attachments':selected,'reference_files':references,'selected_count':len(selected),
        'selected_bytes':size,'unloaded_count':len(references),
        'required_unloaded_count':sum(row['required_unloaded'] for row in references),
        'selection_note':'本轮只预载原始资料、最新纠正与当前交付；历史版本及重复预览可在源任务和资料目录查看。'
            '未预载不等于已阅读。若必需材料尚未载入，分批补读或通过交接说明具体缺口，不让用户重复上传已有文件。'}


def refresh_revision_inputs(db, task):
    """Run in an explicit revision/completion/acquire transaction, never on reads."""
    if not db.in_transaction:raise RuntimeError('Revision input refresh requires a transaction')
    if not task.get('wait_for_task_id') or task['status'] not in {'open','blocked'}:return
    run=db.execute('SELECT state FROM hub_task_executions WHERE task_id=? ORDER BY generation DESC LIMIT 1',(task['id'],)).fetchone()
    if run and run['state'] in {'prepared','running','uncertain'}:return
    rows,_=revision_candidates(db,task)
    keep={row['id'] for row in rows}
    for row in _rows(db,task):
        if row['id'] not in keep:
            db.execute('DELETE FROM hub_task_attachments WHERE task_id=? AND attachment_id=?',(task['id'],row['id']))
    for row in rows:
        db.execute('INSERT OR IGNORE INTO hub_task_attachments(task_id,attachment_id,source_task_id) VALUES (?,?,?)',
                   (task['id'],row['id'],row.get('source_task_id')))


def refresh_waiting_revisions(repo, source_task_id):
    for row in repo.connection.execute("SELECT * FROM tasks WHERE wait_for_task_id=? AND status IN ('open','blocked')",(source_task_id,)).fetchall():
        db=repo.connection;db.execute('SAVEPOINT revision_input_refresh')
        try:refresh_revision_inputs(db,dict(row))
        except Exception as exc:
            db.execute('ROLLBACK TO revision_input_refresh')
            logging.getLogger(__name__).warning('Revision inputs deferred for task %s (%s)',row['id'],type(exc).__name__)
        finally:db.execute('RELEASE revision_input_refresh')
