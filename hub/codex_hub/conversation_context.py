"""Bounded conversation continuity with traceable source material.

Keep original requirements separate from recent task outcomes. A new user turn
must not erase a project brief merely because six newer tasks have appeared.
"""
from __future__ import annotations

import json
import re

MAX_CONTEXT_CHARS = 12000
MAX_INPUT_FILES = 12
MAX_INPUT_BYTES = 100 * 1024 * 1024


def _excerpt(value, limit):
    value = str(value or '')
    if len(value) <= limit:
        return value
    return value[:limit - 180] + '\n[节选；可按 task_id 查阅原文]\n' + value[-140:]


def _public_summary(value):
    return re.sub(r'<(?:hub-reflection|hub-artifacts)\b[^>]*>.*', '', str(value or ''), flags=re.I | re.S).strip()


def conversation_files(db, project_id, conversation_id):
    """Only original bindings in this room, including traceable work results."""
    if not conversation_id:
        return []
    return [dict(row) for row in db.execute('''
        SELECT a.id,a.original_filename,a.content_type,a.byte_size,a.sha256,
               MIN(t.id) AS source_task_id,
               CASE WHEN e.author_user_id IS NOT NULL THEN 'user_input'
                    WHEN EXISTS (SELECT 1 FROM hub_task_artifacts ar WHERE ar.attachment_id=a.id)
                    THEN 'work_result' ELSE 'reference' END AS origin
        FROM hub_task_attachments ta JOIN tasks t ON t.id=ta.task_id
        JOIN attachments a ON a.id=ta.attachment_id LEFT JOIN entries e ON e.id=a.entry_id
        WHERE t.project_id=? AND t.conversation_id=? AND a.project_id=t.project_id
          AND e.deleted_at IS NULL
        GROUP BY a.id ORDER BY a.id DESC''', (project_id, conversation_id))]


def conversation_brief(db, task):
    room_id, project_id = task.get('conversation_id'), task.get('project_id')
    if not room_id:
        return {}
    room = db.execute('SELECT title,purpose FROM hub_conversations WHERE id=? AND project_id=?',
                      (room_id, project_id)).fetchone()
    if not room:
        return {}
    rows = [dict(row) for row in db.execute('''SELECT id,title,description,status,result_summary
        FROM tasks WHERE project_id=? AND conversation_id=? AND created_by_user_id IS NOT NULL AND id<=?
        ORDER BY id DESC LIMIT 20''', (project_id, room_id, task['id']))]
    rows.reverse()
    anchor = db.execute('''SELECT id,title,description,status,result_summary FROM tasks
        WHERE project_id=? AND conversation_id=? AND created_by_user_id IS NOT NULL AND id<=?
        ORDER BY id LIMIT 1''', (project_id, room_id, task['id'])).fetchone()
    # The fixed anchor survives recency eviction. It is a prior interpretation,
    # not a new user instruction or proof that the current run has read a file.
    first = dict(anchor) if anchor else None
    initial = ({'task_id': first['id'], 'user_request': _excerpt(first['description'] or first['title'], 1600),
                'prior_understanding': _excerpt(_public_summary(first['result_summary']), 2400)}
               if first else {'user_request': _excerpt(room['purpose'], 1600)})
    user_updates = []
    remaining = 4000
    for row in reversed(rows):
        if row['id'] == task['id'] or (first and row['id'] == first['id']):
            continue
        value = {'task_id': row['id'], 'user_request': _excerpt(row['description'] or row['title'], 1400),
                 'status': row['status']}
        size = len(json.dumps(value, ensure_ascii=False))
        if size > remaining:
            continue
        user_updates.append(value); remaining -= size
        if len(user_updates) == 8:
            break
    work = [dict(row) for row in db.execute('''
        SELECT t.id AS task_id,t.title,t.status,a.name AS executor
        FROM tasks t JOIN hub_agents a ON a.id=t.assigned_agent_id
        WHERE t.project_id=? AND t.conversation_id=? AND a.kind='worker'
        ORDER BY CASE WHEN t.status IN ('open','claimed','in_progress','blocked') THEN 0 ELSE 1 END,
        t.id DESC LIMIT 10''', (project_id, room_id))]
    bound = {row['attachment_id'] for row in db.execute('SELECT attachment_id FROM hub_task_attachments WHERE task_id=?', (task['id'],))}
    files = conversation_files(db, project_id, room_id)
    # Keep source documents ahead of large preview collections. The full catalog
    # remains available through the separate endpoint and is never auto-read.
    files.sort(key=lambda row: (row['origin'] != 'user_input',
                                row['content_type'] != 'application/pdf', -row['id']))
    catalog = [{key: row[key] for key in ('id','source_task_id','content_type','byte_size','origin')} |
               {'filename': _excerpt(row['original_filename'], 180), 'attached_to_current_task': row['id'] in bound}
               for row in files[:16]]
    result = {'conversation_id': room_id, 'initial_requirement': initial,
              'user_updates': list(reversed(user_updates)), 'work_in_this_conversation': work,
              'reference_files': catalog, 'reference_file_count': len(files),
              'note': '最新用户要求优先；追问延续本对话目标。prior_understanding 是先前理解，不是本轮已重读原文件的证明。'
                      '需要原始材料时用 inherit_attachments 选择本对话目录编号，再实际读取返回的本地文件。'
                      '当前附件与任务交付状态以真实记录为准；不要把局部修图替代尚未完成的总体交付。'}
    while len(json.dumps(result, ensure_ascii=False, separators=(',', ':'))) > MAX_CONTEXT_CHARS:
        if len(result['reference_files']) > 2:
            result['reference_files'].pop()
        elif len(result['work_in_this_conversation']) > 3:
            result['work_in_this_conversation'].pop()
        elif result['user_updates']:
            result['user_updates'].pop(0)
        else:
            break
    result['references_excerpted'] = len(result['reference_files']) < len(files)
    if task.get('controller_read_only'):
        result['note'] = ('最新用户要求优先；这是只读追问，依据实际任务状态与先前理解简短回答。'
                          '先前理解不等于本轮重读原始文件，不能声称已经重新阅读；本轮不继承附件或安排工作。')
    return result


def inherit_attachments(repo, task, device_id, data):
    """Idempotently bind selected room inputs under the current execution fence."""
    from .execution_leases import ExecutionStore, require_execution_fence
    ids = data.get('attachment_ids')
    if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_INPUT_FILES or
            any(type(value) is not int or value <= 0 for value in ids) or len(set(ids)) != len(ids)):
        raise ValueError('请选择 1 至 12 个不重复的本对话附件编号')
    store = ExecutionStore(repo)
    with store.transaction():
        current = repo.get_task(task['id'])
        if not current or current['project_id'] != task['project_id']:
            raise ValueError('当前任务已不存在或不属于本项目')
        require_execution_fence(repo.connection, current, device_id, data.get('execution'), allow_unleased=False)
        agent = repo.connection.execute('SELECT kind FROM hub_agents WHERE id=?', (current['assigned_agent_id'],)).fetchone()
        if not agent or agent['kind'] != 'controller' or not current.get('conversation_id'):
            raise ValueError('只有当前对话总控可以接续参考附件')
        available = {row['id']: row for row in conversation_files(repo.connection, current['project_id'], current['conversation_id'])}
        if not set(ids).issubset(available):
            raise ValueError('参考附件必须来自同一项目的当前对话')
        attached = {row['id']: row for row in repo.list_task_attachments(current['project_id'], current['id'])}
        combined = attached | {value: available[value] for value in ids}
        if len(combined) > MAX_INPUT_FILES or sum(row['byte_size'] for row in combined.values()) > MAX_INPUT_BYTES:
            raise ValueError('当前任务参考材料最多 12 个，总大小最多 100 MiB；请选择必要材料')
        for attachment_id in ids:
            if attachment_id not in attached:
                repo.bind_task_attachments(current['project_id'], [current['id']], [attachment_id],
                                           source_task_id=available[attachment_id]['source_task_id'])
        return {'attachments': repo.list_task_attachments(current['project_id'], current['id'])}
