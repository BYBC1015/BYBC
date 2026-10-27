"""Bounded views for the agent workspace, separate from the project archive.

The workspace reads only the visible conversation. Device connections never
stand in for durable identities, and browsing an identity never dispatches work.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from starlette.responses import Response

from .agents import AgentStore

ENTRY_PAGE_SIZE = 40
ROOM_PAGE_SIZE = 50
ENTRY_BODY_LIMIT = 12_000
PAGE_BODY_BUDGET = 120_000
WORKSPACE_MAX_FILES = 12
WORKSPACE_MAX_FILE_BYTES = 50 * 1024 * 1024
WORKSPACE_MAX_TOTAL_BYTES = 100 * 1024 * 1024


def workspace_upload_limits(request: Request) -> dict:
    from .main import get_settings
    return {'max_files': WORKSPACE_MAX_FILES,
            'max_file_bytes': min(get_settings(request).max_upload_bytes, WORKSPACE_MAX_FILE_BYTES),
            'max_total_bytes': WORKSPACE_MAX_TOTAL_BYTES}


async def inspect_workspace_uploads(request: Request, uploads) -> list[dict]:
    """Hash bounded spooled uploads before storing, so exact retries write no files."""
    from .main import safe_content_type, safe_upload_filename
    limits = workspace_upload_limits(request)
    if len(uploads) > limits['max_files']:
        raise ValueError(f"每条工作最多上传 {limits['max_files']} 个文件")
    manifest, total = [], 0
    for upload in uploads:
        digest, size = hashlib.sha256(), 0
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            total += len(chunk)
            if size > limits['max_file_bytes']:
                raise HTTPException(413, '单个文件超过上传大小限制，请缩小后重试')
            if total > limits['max_total_bytes']:
                raise HTTPException(413, '本条工作的附件总大小超过限制，请分批安排')
            digest.update(chunk)
        manifest.append({'original_filename': safe_upload_filename(upload.filename),
                         'content_type': safe_content_type(upload.content_type),
                         'byte_size': size, 'sha256': digest.hexdigest()})
        await upload.seek(0)
    return manifest


def _cursor(value: str) -> int | None:
    if not value.isascii() or not value.isdigit() or len(value) > 18:
        return None
    return int(value) or None


def _room_views(repo, project_id: int, before: int | None):
    clause = ' AND c.id<?' if before else ''
    params = (project_id, before, ROOM_PAGE_SIZE + 1) if before else (project_id, ROOM_PAGE_SIZE + 1)
    rows = [dict(row) for row in repo.connection.execute(f"""SELECT c.id,c.project_id,c.title,c.status,c.created_at,
        (SELECT group_concat(a.name,'、') FROM hub_conversation_agents ca
         JOIN hub_agents a ON a.id=ca.agent_id WHERE ca.conversation_id=c.id) AS participants
        FROM hub_conversations c WHERE c.project_id=?{clause} ORDER BY c.id DESC LIMIT ?""", params)]
    has_older = len(rows) > ROOM_PAGE_SIZE
    rows = rows[:ROOM_PAGE_SIZE]
    total = repo.connection.execute('SELECT COUNT(*) FROM hub_conversations WHERE project_id=?',
                                    (project_id,)).fetchone()[0]
    return rows, rows[-1]['id'] if has_older else None, total


def _task_views(repo, project_id: int, *, conversation_id: int | None = None,
                limit: int = 50, board: str = 'all', before: int | None = None,
                task_ids: list[int] | None = None) -> list[dict[str, Any]]:
    clause = " AND t.conversation_id=?" if conversation_id is not None else ""
    params = [project_id,conversation_id] if conversation_id is not None else [project_id]
    from .task_board import active_sql,can_archive
    if board!='all':clause+=' AND '+('NOT ' if board=='history' else '')+active_sql()
    if before:clause+=' AND t.id<?';params.append(before)
    if task_ids is not None:
        if not task_ids:return []
        clause+=' AND t.id IN ('+','.join('?' for _ in task_ids)+')';params.extend(task_ids)
    params.append(limit)
    # Exclude large context packs and bound the previews at the database boundary.
    rows = repo.connection.execute(f"""SELECT t.id,t.project_id,t.title,t.status,t.priority,
        t.assigned_agent_id,t.assigned_device_id,t.conversation_id,t.created_at,t.updated_at,
        t.status_version,t.route_engine,t.route_reason,t.wait_for_task_id,t.coordination_state,t.execution_mode,t.board_state,
        NOT {active_sql()} AS in_history,substr(t.description,1,2000) AS description,
        substr(t.result_summary,1,2000) AS result_summary,
        a.name AS assigned_agent_name,a.kind AS assigned_agent_kind,
        d.label AS assigned_device_label,c.title AS conversation_title,mc.selection_json AS model_selection_json
        FROM tasks t LEFT JOIN hub_agents a ON a.id=t.assigned_agent_id
        LEFT JOIN devices d ON d.id=t.assigned_device_id
        LEFT JOIN hub_conversations c ON c.id=t.conversation_id
        LEFT JOIN hub_task_model_choices mc ON mc.task_id=t.id
        WHERE t.project_id=?{clause} ORDER BY t.id DESC LIMIT ?""", params)
    result = [dict(row) for row in rows]
    for task in result:
        task['model_selection'] = json.loads(task.pop('model_selection_json') or 'null')
        from .execution_leases import latest_execution
        task['archive_eligible']=can_archive(task,latest_execution(repo.connection,task['id']))
        task['board_label']='已回复' if task['assigned_agent_kind']=='controller' and task['status']=='review' else ''
    return result


def _entry_views(request: Request, repo, project: dict, conversation_id: int, before: int | None):
    from .main import app_path

    cursor = repo.connection.execute('SELECT created_at FROM entries WHERE id=? AND project_id=?',(before,project['id'])).fetchone() if before else None
    clause = " AND (e.created_at<? OR (e.created_at=? AND e.id<?))" if cursor else ""
    params = (project['id'], conversation_id, cursor['created_at'],cursor['created_at'],before, ENTRY_PAGE_SIZE + 1) if cursor else (
        project['id'], conversation_id, ENTRY_PAGE_SIZE + 1)
    rows = [dict(row) for row in repo.connection.execute(f"""SELECT
        e.id,e.project_id,e.task_id,e.author_type,e.author_user_id,e.author_device_id,
        e.source_label,e.kind,e.created_at,e.updated_at,
        substr(e.body_markdown,1,{ENTRY_BODY_LIMIT}) AS body_markdown,
        length(e.body_markdown) AS body_length,t.title AS task_title,
        t.assigned_agent_id,t.status AS task_status,t.wait_for_task_id,t.coordination_state,a.kind AS agent_kind,a.name AS agent_name,a.name AS assigned_agent_name
        FROM entries e JOIN tasks t ON t.id=e.task_id
        LEFT JOIN hub_agents a ON a.id=t.assigned_agent_id
        WHERE e.project_id=? AND t.conversation_id=? AND e.deleted_at IS NULL{clause}
        ORDER BY e.created_at DESC,e.id DESC LIMIT ?""", params)]
    has_older = len(rows) > ENTRY_PAGE_SIZE
    rows = rows[:ENTRY_PAGE_SIZE]
    # Prioritize the most recent result without permitting one page to grow
    # indefinitely when a worker returns a large document.
    budget = PAGE_BODY_BUDGET
    for row in rows:
        body = row['body_markdown'][:max(0, budget)]
        budget -= len(body)
        row['body_markdown'] = body
        row['body_truncated'] = row.pop('body_length') > len(body)
        row['record_url'] = app_path(request, f"/projects/{project['slug']}/tasks/{row['task_id']}")
        row['attachments'] = []
    attachment_rows = repo.list_attachments_for_entries(project['id'], [row['id'] for row in rows])
    entries_by_id = {row['id']: row for row in rows}
    seen_artifacts = set()
    for attachment in attachment_rows:
        entry = entries_by_id[attachment['entry_id']]
        identity = (entry['id'], attachment['original_filename'], attachment['sha256'])
        if entry['author_type'] != 'user' and attachment['sha256']:
            if identity in seen_artifacts:
                continue
            seen_artifacts.add(identity)
        if len(entry['attachments']) < 12:
            entry['attachments'].append({
                'id': attachment['id'], 'original_filename': attachment['original_filename'],
                'byte_size': attachment['byte_size'], 'content_type': attachment['content_type'],
                'url': app_path(request, f"/projects/{project['slug']}/attachments/{attachment['id']}"),
                'preview_url': app_path(request, f"/projects/{project['slug']}/attachments/{attachment['id']}?inline=true&size=preview")
                    if attachment['content_type'] in {'image/png','image/jpeg','image/gif','image/webp','image/avif'} else '',
                'text_preview_url': app_path(request, f"/projects/{project['slug']}/attachments/{attachment['id']}/preview")
                    if PurePosixPath(attachment['original_filename']).suffix.lower()=='.pdf' or
                    (PurePosixPath(attachment['original_filename']).suffix.lower() in {'.md','.txt','.csv','.json','.log'} and attachment['byte_size']<=1024*1024) else '',
            })
    for row in rows:
        row['view_version']=hashlib.sha256(json.dumps([row['body_markdown'],row['attachments'],row['task_status'],row['coordination_state']],sort_keys=True).encode()).hexdigest()[:16]
        row['receipt_label']={'open':'已收到 · 等待总控回应' if row['agent_kind']=='controller' else '已收到 · 等待执行',
            'claimed':'已收到 · 准备处理','in_progress':'已收到 · 正在处理','review':'已回复 · 可查看结果',
            'blocked':'已收到 · 当前遇阻','done':'已完成','cancelled':'已取消'}.get(row['task_status'],'已收到')
        if row['coordination_state']=='waiting' and row['task_status'] not in {'done','cancelled'}:
            row['receipt_label']='正在交接 · 等待总控答复'
    oldest_id = rows[-1]['id'] if rows else None
    return list(reversed(rows)), oldest_id if has_older else None


def build_workspace_context(request: Request, user: dict, project: dict, *,
                            conversation_id: int | None = None,
                            dispatch_error: str = '', dispatch_draft: dict | None = None) -> dict:
    from .main import annotate_membership_presence, app_path, get_repo

    repo = get_repo(request)
    store = AgentStore(repo.connection)
    room = None
    if conversation_id is not None:
        found = repo.connection.execute('SELECT * FROM hub_conversations WHERE id=? AND project_id=?',
                                       (conversation_id, project['id'])).fetchone()
        if not found:
            raise HTTPException(404, '对话不存在')
        room = dict(found)

    agents = store.list(project['id'])
    executable_ids = {row['id'] for row in repo.connection.execute("""SELECT a.id FROM hub_agents a
        JOIN devices d ON d.id=a.device_id JOIN project_memberships m ON m.device_id=d.id
        WHERE a.project_id=? AND m.project_id=a.project_id AND a.active=1 AND d.is_active=1
        AND m.permission IN ('write','admin') AND d.server_ai_provider=''""", (project['id'],))}
    for agent in agents:
        agent['can_dispatch'] = agent['id'] in executable_ids
    controller = next((a for a in agents if a['kind'] == 'controller' and a['active']), None)
    default_agent = controller or next((a for a in agents if a['can_dispatch']), None)
    selected_agent = default_agent
    agent_id = _cursor(request.query_params.get('agent', ''))
    if agent_id:
        selected_agent = next((a for a in agents if a['id'] == agent_id), default_agent)
    # This separate, explicitly labelled action may preselect a recipient;
    # ordinary identity browsing above must never change the recipient.
    raw_assignment = request.query_params.get('assign_agent', '')
    if raw_assignment:
        assignment_id = _cursor(raw_assignment)
        default_agent = next((a for a in agents if a['id'] == assignment_id), None)
        selected_agent = default_agent or selected_agent
    default_agent_id = default_agent['id'] if default_agent and default_agent['can_dispatch'] else None
    before = _cursor(request.query_params.get('before', ''))
    rooms_before = _cursor(request.query_params.get('rooms_before', ''))
    entries, older_id = _entry_views(request, repo, project, conversation_id, before) if room else ([], None)
    task_history=request.query_params.get('task_history')=='1'
    task_before=_cursor(request.query_params.get('tasks_before','')) if task_history else None
    tasks = _task_views(repo, project['id'], conversation_id=conversation_id, limit=31 if task_history else 60,
                        board='history' if task_history else 'active',before=task_before) if room else []
    more_history=len(tasks)>30 if task_history else False
    if task_history:tasks=tasks[:30]
    workspace_tasks = _task_views(repo, project['id'],board='active')
    message_tasks=_task_views(repo,project['id'],task_ids=list({entry['task_id'] for entry in entries}),limit=40)
    from .task_board import active_sql
    history_count=repo.connection.execute('SELECT COUNT(*) FROM tasks t WHERE t.project_id=? AND t.conversation_id=? AND NOT '+active_sql(),
                                         (project['id'],conversation_id)).fetchone()[0] if room else 0
    from .execution_context import annotate_execution
    memberships = annotate_membership_presence(repo.list_project_memberships(project['id']))
    annotate_execution(agents, tasks + workspace_tasks + message_tasks, memberships)
    from .device_capabilities import load_device_capabilities
    capabilities = load_device_capabilities(request.app.state.settings.data_dir,
                                           {a['device_id'] for a in agents if a.get('device_id')})
    for agent in agents:
        agent['connection_machine'] = capabilities.get(agent.get('device_id'), {}).get('machine_name', '')
    for task in tasks + workspace_tasks + message_tasks:
        task['detail_url'] = app_path(request, f"/projects/{project['slug']}/tasks/{task['id']}")
    conversations, older_room_id, room_total = _room_views(repo, project['id'], rooms_before)
    room_page_count = len(conversations)
    if room and not any(c['id'] == room['id'] for c in conversations):
        conversations.append({**room, 'participants': '', 'outside_room_page': True})
        conversations.sort(key=lambda c: c['id'], reverse=True)
    participant_ids = {row['agent_id'] for row in repo.connection.execute(
        'SELECT agent_id FROM hub_conversation_agents WHERE conversation_id=?', (conversation_id,))} if room else set()
    for conversation in conversations:
        if conversation.get('outside_room_page'):
            conversation['participants'] = '、'.join(a['name'] for a in agents if a['id'] in participant_ids)
        conversation['url'] = app_path(request, f"/projects/{project['slug']}/conversations/{conversation['id']}")
    projects = repo.list_projects()
    dispatch_key = (dispatch_draft or {}).get('request_key') or uuid.uuid4().hex
    draft = dispatch_draft if dispatch_draft is not None else {
        'description': '', 'title': '', 'agent_ids': [default_agent_id] if default_agent_id else [],
        'conversation_id': conversation_id, 'request_key': dispatch_key,
    }
    room_url = app_path(request, f"/projects/{project['slug']}/conversations/{conversation_id}") if room else ''
    page_url = room_url or app_path(request, f"/projects/{project['slug']}")
    refresh_query = {name: request.query_params[name] for name in ('before', 'rooms_before', 'agent', 'assign_agent','task_history','tasks_before')
                     if request.query_params.get(name)}
    def page_link(**changes):
        query = {**refresh_query, **changes}
        query = {name: value for name, value in query.items() if value is not None}
        return page_url + ('?' + urlencode(query) if query else '')

    workspace_url = page_link()
    return {
        'project': project, 'user': user, 'projects': projects, 'projectlist': projects,
        'durable_agents': agents, 'agent_conversations': conversations,
        'memberships': memberships,
        'current_conversation': room, 'selected_conversation': room,
        'participants': [a for a in agents if a['id'] in participant_ids],
        'tasks': tasks, 'conversation_tasks': tasks, 'workspace_tasks': workspace_tasks,
        'message_tasks':message_tasks,'task_history':task_history,'task_history_count':history_count,
        'task_history_url':page_link(task_history='1',tasks_before=None),
        'task_active_url':page_link(task_history=None,tasks_before=None),
        'older_tasks_url':page_link(task_history='1',tasks_before=tasks[-1]['id']) if more_history else '',
        'entries': entries, 'conversation_entries': entries,
        'default_agent_id': default_agent_id, 'selected_agent': selected_agent,
        'role_routing_enabled': any(a.get('role_key') == 'controller' for a in agents),
        'dispatch_key': dispatch_key, 'dispatch_draft': draft, 'dispatch_error': dispatch_error,
        'older_entries_url': page_link(before=older_id) if older_id else '',
        'reading_newer_url': page_link(before=None) if before else '',
        'older_rooms_url': page_link(rooms_before=older_room_id) if older_room_id else '',
        'latest_rooms_url': page_link(rooms_before=None) if rooms_before else '',
        'room_page_count': room_page_count, 'room_total_count': room_total,
        'rooms_has_older': bool(older_room_id),
        'workspace_url': workspace_url,
        'workspace_upload_limits': workspace_upload_limits(request),
    }


def render_workspace(request: Request, user: dict, project: dict, *,
                     conversation_id: int | None = None,
                     dispatch_error: str = '', dispatch_draft: dict | None = None):
    from .main import csrf_token, render

    context = build_workspace_context(request, user, project, conversation_id=conversation_id,
                                      dispatch_error=dispatch_error, dispatch_draft=dispatch_draft)
    # The nonce is intentionally excluded: refreshing must not create a new
    # dispatch intent or defeat conditional requests on an unchanged room.
    versioned = {k: v for k, v in context.items() if k not in {'dispatch_key', 'dispatch_draft'}}
    versioned['csrf_token'] = csrf_token(request)
    from . import __version__
    versioned['view_version'] = __version__
    etag = 'W/"' + hashlib.sha256(json.dumps(versioned, ensure_ascii=False, sort_keys=True,
                                           default=str).encode()).hexdigest() + '"'
    headers = {'ETag': etag, 'Cache-Control': 'private, no-cache', 'Vary': 'Cookie'}
    candidates = [value.strip() for value in request.headers.get('if-none-match', '').split(',')]
    if request.method == 'GET' and not dispatch_error and etag in candidates:
        return Response(status_code=304, headers=headers)
    context['workspace_etag'] = etag
    response = render(request, 'project_workspace.html', **context)
    response.headers.update(headers)
    return response
