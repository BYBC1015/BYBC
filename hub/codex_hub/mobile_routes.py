"""Session-authenticated data API for locally rendered mobile clients.

Navigation is owned by the installed app. These routes never render or redirect
to Hub pages, and reading data never claims, retries or dispatches a task.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from .agents import AgentStore, dispatch_payload_hash
from .workspace import (
    build_workspace_context, inspect_workspace_uploads, WORKSPACE_MAX_FILES,
    _task_views, workspace_upload_limits,
)

KEY_RE = re.compile(r'[A-Za-z0-9_.-]{8,100}')
STATUS_LABELS = {'open': '等待执行', 'claimed': '准备处理', 'in_progress': '正在处理',
                 'review': '已交付 · 待查看', 'done': '已完成', 'blocked': '遇到阻碍',
                 'cancelled': '已取消'}


def pick(row, names):
    return {name: row.get(name) for name in names.split()}


def positive(value, *, zero=False):
    if value is None or value == '':
        return None
    if isinstance(value, bool) or not re.fullmatch(r'[0-9]{1,18}', str(value)):
        raise HTTPException(422, '编号无效')
    result = int(value)
    if result == 0 and not zero:
        raise HTTPException(422, '编号无效')
    return result


async def object_body(request):
    if request.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
        raise HTTPException(415, '请使用 JSON 数据请求')
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 128 * 1024:
            raise HTTPException(413, '请求内容过长')
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(422, 'JSON 数据无效') from exc
    if not isinstance(value, dict):
        raise HTTPException(422, '请求必须是对象')
    return value


def session_data(request, user):
    from . import __version__
    from .main import get_settings, csrf_token
    raw = request.cookies.get(get_settings(request).session_cookie_name, '')
    return {'api_version': 1, 'release': __version__,
            'user': pick(user, 'id username'), 'csrf_token': csrf_token(request),
            'session_key': hashlib.sha256(('native-mobile-v1:' + raw).encode()).hexdigest()}


def attachment_data(request, slug, row):
    from .main import app_path
    from .media_assets import media_type_for
    url = app_path(request, f'/projects/{slug}/attachments/{row["id"]}')
    name = row.get('original_filename') or row.get('name') or '文件'
    image = media_type_for({'original_filename': name, 'content_type': row.get('content_type', '')}) == 'image'
    return {'id': row['id'], 'name': name, 'mime_type': row.get('content_type') or 'application/octet-stream',
            'byte_size': row.get('byte_size', 0), 'download_url': url,
            'preview_url': url + '?inline=true&size=preview' if image else ''}


def message_data(request, slug, row):
    role = 'user' if row.get('author_type') == 'user' else 'system' if row.get('kind') == 'system' else 'assistant'
    return {'id': row['id'], 'author': '你' if role == 'user' else row.get('agent_name') or row.get('source_label') or '智能体',
            'role': role, 'text': row.get('body_markdown', ''), 'created_at': row.get('created_at', ''),
            'task_id': row.get('task_id'), 'receipt_label': row.get('receipt_label', ''),
            'body_truncated': bool(row.get('body_truncated')),
            'attachments': [attachment_data(request, slug, item) for item in row.get('attachments', [])]}


def task_data(row):
    data = pick(row, 'id title description status priority conversation_id assigned_agent_id assigned_agent_name '
                'assigned_device_id assigned_device_label result_summary board_label execution_label execution_hint '
                'execution_route execution_progress created_at updated_at in_history archive_eligible')
    data['status_label'] = row.get('board_label') or STATUS_LABELS.get(row['status'], row['status'])
    data['in_history'] = bool(row.get('in_history'))
    data['archive_eligible'] = bool(row.get('archive_eligible'))
    return data


def device_data(row):
    return pick(row, 'device_id device_label is_online is_agent_online presence_label last_seen_at last_agent_seen_at')


def page_cursor(url, key):
    return positive(parse_qs(urlsplit(url or '').query).get(key, [None])[0])


def register_mobile_routes(app):
    from .main import (get_repo, get_settings, require_user, current_user, require_project,
                       require_project_task, verify_csrf_token, persist_session_cookies,
                       store_attachment_file, remove_stored_attachment, annotate_membership_presence)

    def scope(request, slug):
        user = require_user(request)
        repo = get_repo(request)
        return user, repo, require_project(repo, slug)

    def csrf(request, data=None):
        verify_csrf_token(request, request.headers.get('x-csrf-token') or str((data or {}).get('_csrf', '')))

    @app.get('/api/mobile/session')
    def mobile_session(request: Request):
        user = require_user(request)
        value = session_data(request, user)
        settings, repo = get_settings(request), get_repo(request)
        raw = request.cookies.get(settings.session_cookie_name, '')
        token = value['csrf_token']
        if not token or not repo.session_matches_csrf(raw, token):
            token = repo.rotate_session_csrf(raw)
            if not token:
                raise HTTPException(401, '登录已过期，请重新登录')
            value['csrf_token'] = token
        response = JSONResponse(value)
        # Keep the native cookie expiry synchronized with the existing session.
        persist_session_cookies(request, response, raw, token)
        return response

    @app.post('/api/mobile/login')
    async def mobile_login(request: Request):
        data = await object_body(request)
        username, password = data.get('username'), data.get('password')
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 200 or len(password) > 4096:
            raise HTTPException(422, '请输入有效的账号和密码')
        repo = get_repo(request)
        user = repo.authenticate_user(username.strip(), password)
        if not user:
            raise HTTPException(401, '账号或密码不正确')
        session = repo.create_session(user['id'])
        value = session_data(request, user)
        value.update(csrf_token=session.csrf,
                     session_key=hashlib.sha256(('native-mobile-v1:' + session.raw).encode()).hexdigest())
        response = JSONResponse(value)
        persist_session_cookies(request, response, session.raw, session.csrf)
        return response

    @app.post('/api/mobile/logout')
    async def mobile_logout(request: Request):
        require_user(request)
        csrf(request)
        settings = get_settings(request)
        get_repo(request).delete_session(request.cookies.get(settings.session_cookie_name, ''))
        response = JSONResponse({'ok': True})
        response.delete_cookie(settings.session_cookie_name)
        response.delete_cookie(settings.csrf_cookie_name)
        return response

    @app.get('/api/mobile/projects')
    def mobile_projects(request: Request):
        user = require_user(request)
        return {**session_data(request, user), 'projects': [pick(row, 'id slug name description')
                for row in get_repo(request).list_projects()]}

    @app.get('/api/mobile/projects/{slug}/workspace')
    def mobile_workspace(request: Request, slug: str):
        user, repo, project = scope(request, slug)
        raw_room = request.query_params.get('conversation_id')
        room_id = positive(raw_room, zero=True)
        if raw_room is None:
            latest = repo.connection.execute('SELECT id FROM hub_conversations WHERE project_id=? ORDER BY id DESC LIMIT 1',
                                             (project['id'],)).fetchone()
            room_id = latest['id'] if latest else None
        # The shared context is a read model only; templates and links never leave this API.
        if request.query_params.get('before_entry_id') and not request.query_params.get('before'):
            query = dict(request.query_params)
            query['before'] = query.pop('before_entry_id')
            from urllib.parse import urlencode
            clone_scope = {**request.scope, 'query_string': urlencode(query).encode()}
            request = Request(clone_scope, request.receive)
        for name in ('before', 'rooms_before'):
            if name in request.query_params:
                positive(request.query_params[name])
        before = positive(request.query_params.get('before'))
        if before and (not room_id or not repo.connection.execute('''SELECT 1 FROM entries e
                JOIN tasks t ON t.id=e.task_id WHERE e.id=? AND e.project_id=? AND t.conversation_id=?
                AND e.deleted_at IS NULL''', (before, project['id'], room_id)).fetchone()):
            raise HTTPException(404, '历史消息位置不属于当前对话')
        context = build_workspace_context(request, user, project, conversation_id=room_id or None)
        messages = [message_data(request, slug, row) for row in context['entries']]
        value = {**session_data(request, user), 'project': pick(project, 'id slug name description'),
                 'conversation': pick(context['current_conversation'], 'id title status') if context['current_conversation'] else None,
                 'conversations': [pick(row, 'id title status participants created_at') for row in context['agent_conversations']],
                 'agents': [pick(row, 'id name job instructions kind role_key active can_dispatch device_id '
                                  'connection_state connection_label connection_hint') for row in context['durable_agents']],
                 'tasks': [task_data(row) for row in context['workspace_tasks']],
                 'entries': messages, 'messages': messages, 'devices': [device_data(row) for row in context['memberships']],
                 'progress': context['conversation_progress'], 'default_agent_id': context['default_agent_id'],
                 'older_entries_before': page_cursor(context['older_entries_url'], 'before'),
                 'older_rooms_before': page_cursor(context['older_rooms_url'], 'rooms_before'),
                 'upload_limits': workspace_upload_limits(request)}
        for agent in value['agents']:
            agent['active'] = bool(agent['active'])
            agent['can_dispatch'] = bool(agent['can_dispatch'])
        # Do not return web routes or unstable render nonces. Clients can skip unchanged renders.
        for item in value['progress']:
            item.pop('detail_url', None)
        value['revision'] = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
        return value

    @app.get('/api/mobile/projects/{slug}/tasks')
    def mobile_tasks(request: Request, slug: str):
        user, repo, project = scope(request, slug)
        before = positive(request.query_params.get('before'))
        rows = _task_views(repo, project['id'], board='history' if request.query_params.get('history') == '1' else 'active',
                           before=before, limit=51)
        return {'tasks': [task_data(row) for row in rows[:50]], 'older_before': rows[49]['id'] if len(rows) > 50 else None}

    @app.get('/api/mobile/projects/{slug}/tasks/{task_id}')
    def mobile_task(request: Request, slug: str, task_id: int):
        user, repo, project = scope(request, slug)
        require_project_task(repo, project, task_id)
        task = _task_views(repo, project['id'], task_ids=[task_id], limit=1)[0]
        before = positive(request.query_params.get('before'))
        clause, args = (' AND e.id<?', [before]) if before else ('', [])
        rows = [dict(row) for row in repo.connection.execute(f'''SELECT e.id,e.task_id,e.author_type,
            e.source_label,e.kind,e.created_at,substr(e.body_markdown,1,12000) AS body_markdown,
            length(e.body_markdown)>12000 AS body_truncated,a.name AS agent_name
            FROM entries e JOIN tasks t ON t.id=e.task_id LEFT JOIN hub_agents a ON a.id=t.assigned_agent_id
            WHERE e.project_id=? AND e.task_id=? AND e.deleted_at IS NULL{clause} ORDER BY e.id DESC LIMIT 41''',
            [project['id'], task_id, *args])]
        older = rows[39]['id'] if len(rows) > 40 else None
        rows = rows[:40]
        attachments = repo.list_attachments_for_entries(project['id'], [row['id'] for row in rows])
        budget = 120000
        for row in rows:
            text = row['body_markdown'][:max(0, budget)]
            row['body_truncated'] = row['body_truncated'] or len(text) != len(row['body_markdown'])
            row['body_markdown'] = text
            budget -= len(text)
            row['attachments'] = [item for item in attachments if item['entry_id'] == row['id']][:12]
        from .execution_progress import active_progress
        messages = [message_data(request, slug, row) for row in reversed(rows)]
        return {'task': task_data(task), 'entries': messages, 'messages': messages,
                'progress': active_progress(repo.connection, project['id'], task_ids=[task_id], include_expired=True),
                'older_entries_before': older}

    @app.get('/api/mobile/projects/{slug}/devices')
    def mobile_devices(request: Request, slug: str):
        _, repo, project = scope(request, slug)
        return {'devices': [device_data(row) for row in annotate_membership_presence(repo.list_project_memberships(project['id']))]}

    @app.get('/api/mobile/projects/{slug}/library')
    def mobile_library(request: Request, slug: str):
        _, repo, project = scope(request, slug)
        before = positive(request.query_params.get('before'))
        rows = [dict(row) for row in repo.connection.execute('SELECT * FROM attachments WHERE project_id=?' +
                (' AND id<?' if before else '') + ' ORDER BY id DESC LIMIT 51',
                [project['id'], *([before] if before else [])])]
        return {'attachments': [attachment_data(request, slug, row) for row in rows[:50]],
                'older_before': rows[49]['id'] if len(rows) > 50 else None}

    @app.get('/api/mobile/toolbox')
    def mobile_toolbox(request: Request):
        require_user(request)
        return {'tools': [dict(id=action, action=action, title=title, description=description) for action, title, description in (
            ('library', '项目文件', '查看、预览与保存项目中的图片和文件'),
            ('devices', '执行设备', '查看电脑连接状态和最近心跳'),
            ('feedback', '问题与建议', '记录遇到的问题，自动附上设备和项目线索'),
            ('downloads', '检查更新', '查看可用安装包和当前手机版本'))]}

    @app.get('/api/mobile/projects/{slug}/receipts/{request_key}')
    def mobile_receipt(request: Request, slug: str, request_key: str):
        user, repo, project = scope(request, slug)
        if not KEY_RE.fullmatch(request_key):
            raise HTTPException(422, '请求标识无效')
        rows = repo.connection.execute('''SELECT d.conversation_id,t.id,t.created_by_user_id
            FROM hub_agent_dispatches d JOIN hub_dispatch_tasks dt ON dt.dispatch_id=d.id JOIN tasks t ON t.id=dt.task_id
            WHERE d.project_id=? AND d.request_key=? AND t.project_id=d.project_id ORDER BY t.id''',
            (project['id'], f'native:{user["id"]}:{request_key}')).fetchall()
        if not rows or any(row['created_by_user_id'] != user['id'] for row in rows):
            return {'found': False, 'request_key': request_key}
        return {'found': True, 'request_key': request_key, 'conversation_id': rows[0]['conversation_id'],
                'task_ids': [row['id'] for row in rows]}

    @app.post('/api/mobile/projects/{slug}/messages')
    async def mobile_message(request: Request, slug: str):
        user, repo, project = scope(request, slug)
        store = AgentStore(repo.connection)
        form, stored, keep = None, [], False
        try:
            if request.headers.get('content-type', '').split(';')[0].strip() == 'application/json':
                data = await object_body(request)
                ids, uploads = data.get('agent_ids', []), []
                if data.get('attachments') or data.get('attachment_ids'):
                    raise HTTPException(422, '附件须与消息一起通过文件上传发送')
            else:
                form = await request.form(max_files=WORKSPACE_MAX_FILES + 1, max_fields=50)
                data = dict(form)
                ids = form.getlist('agent_ids')
                uploads = [value for name, value in form.multi_items() if name == 'attachments' and isinstance(value, UploadFile) and value.filename]
                if any(isinstance(value, UploadFile) and name != 'attachments' for name, value in form.multi_items()):
                    raise HTTPException(422, '附件字段无效')
            csrf(request, data)
            description, title, key = data.get('description', ''), data.get('title', ''), data.get('request_key', '')
            if not all(isinstance(value, str) for value in (description, title, key)):
                raise HTTPException(422, '消息内容无效')
            if len(description) > 16000 or len(title) > 200 or not KEY_RE.fullmatch(key):
                raise HTTPException(422, '消息过长或请求标识无效')
            if not isinstance(ids, list) or not 1 <= len(ids) <= 12:
                raise HTTPException(422, '请选择本次工作的智能体')
            ids = [positive(value) for value in ids]
            if any(value is None for value in ids):
                raise HTTPException(422, '请选择有效智能体')
            room_id = positive(data.get('conversation_id'), zero=True) or None
            if room_id and not repo.connection.execute('SELECT 1 FROM hub_conversations WHERE id=? AND project_id=?',
                                                       (room_id, project['id'])).fetchone():
                raise HTTPException(404, '对话不存在')
            description = description.strip()
            if not description and not uploads:
                raise HTTPException(422, '请输入消息或添加附件')
            manifest = await inspect_workspace_uploads(request, uploads)
            # Upload parsing/hashing can outlive logout on another request. The
            # original cookie bytes are not evidence the session is still valid.
            if require_user(request)['id'] != user['id']:
                raise HTTPException(401, '登录已改变，请保留草稿后重新登录')
            csrf(request, data)
            title = title.strip() or ' '.join(description.split())[:80] or ('处理附件：' + manifest[0]['original_filename'])[:80]
            full_key = f'native:{user["id"]}:{key}'
            digest = dispatch_payload_hash(ids, title, description, room_id, attachments=manifest)
            try:
                result = store.previous_dispatch(repo, project['id'], full_key, digest)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            if result is None:
                for agent_id in ids:
                    store.assignment(project['id'], agent_id)
                if room_id and not repo.connection.execute("SELECT 1 FROM hub_conversations WHERE id=? AND project_id=? AND status='active'",
                                                          (room_id, project['id'])).fetchone():
                    raise HTTPException(409, '对话已结束，请选择新对话')
                for upload in uploads:
                    stored.append(await store_attachment_file(request, project, upload))
                    if require_user(request)['id'] != user['id']:
                        raise HTTPException(401, '登录已改变，请保留草稿后重新登录')
                    csrf(request, data)
                result = store.dispatch(repo, project['id'], agent_ids=ids, title=title, description=description,
                                        request_key=full_key, conversation_id=room_id, user_id=user['id'], attachments=stored)
                keep = result['created']
            return {'created': result['created'], 'request_key': key, 'conversation_id': result['conversation_id'],
                    'task_ids': [task['id'] for task in result['tasks']]}
        except (ValueError, TypeError) as exc:
            raise HTTPException(409 if '同一请求' in str(exc) else 422, str(exc)) from exc
        finally:
            try:
                if not keep:
                    for item in stored:
                        remove_stored_attachment(request, item)
            finally:
                if form is not None:
                    await form.close()

    @app.post('/api/mobile/projects/{slug}/feedback')
    async def mobile_feedback(request: Request, slug: str):
        user, repo, project = scope(request, slug)
        data = await object_body(request)
        csrf(request, data)
        from .maintenance import MaintenanceStore, capture_context
        try:
            context = capture_context(repo, project['id'], request, data, get_settings(request).data_dir)
            result = MaintenanceStore(repo).create_feedback(project['id'], user['id'], kind=data.get('kind', 'bug'),
                title=data.get('title', ''), body=data.get('body', ''), reproduction=data.get('reproduction', ''),
                expected=data.get('expected', ''), context=context, request_key=data.get('request_key', ''))
            return {'feedback': pick(result, 'id status title')}
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc
