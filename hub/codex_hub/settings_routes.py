"""Cookie-authenticated settings data for the web and packaged workspace.

These endpoints return JSON exclusively; they never navigate into the archive.
Reading settings does not create, dispatch, or retry execution work.
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from .agents import AgentStore
from .mobile_routes import object_body, positive
from .model_policy import agent_policy, available_models, reserve_policy, save_policy


def register_settings_routes(app):
    from .main import (annotate_membership_presence, get_repo, get_settings,
                       require_device, require_project, require_user, verify_csrf_token)

    def context(request, slug):
        user = require_user(request)
        repo = get_repo(request)
        return user, repo, require_project(repo, slug)

    def authorize_write(request, data=None, *, csrf=True):
        user = require_user(request)
        if user.get('role') != 'owner':
            raise HTTPException(403, '只有管理员可以修改项目设置')
        if csrf:
            verify_csrf_token(request, request.headers.get('x-csrf-token', '') or str((data or {}).get('_csrf') or ''))
        return user

    def project_data(user, repo, project):
        members = annotate_membership_presence(repo.list_project_memberships(project['id']))
        attached = {item['device_id'] for item in members}
        primary = repo.get_project_primary_device(project['id'])
        return {
            'project': {key: project.get(key) for key in ('id', 'slug', 'name', 'description')},
            'can_manage': user.get('role') == 'owner',
            'primary_device_id': primary['id'] if primary else None,
            'members': [{key: member.get(key) for key in (
                'device_id', 'device_label', 'permission', 'is_agent_online', 'presence_label',
                'server_ai_provider', 'agent_tasks_only')} for member in members],
            'available_devices': [{'id': device['id'], 'label': device['label']}
                                  for device in repo.list_devices()
                                  if device['id'] not in attached and not device.get('server_ai_provider')],
            'agents': [{'id': agent['id'], 'name': agent['name'], 'role': agent.get('role_key') or agent['kind']}
                       for agent in AgentStore(repo.connection).list(project['id'])],
        }

    def get_agent(repo, project, agent_id):
        try:
            return AgentStore(repo.connection).get(project['id'], agent_id)
        except ValueError:
            raise HTTPException(404, '智能体不存在') from None

    def policy_data(request, repo, project, agent):
        return {'agent_id': agent['id'], 'policy': agent_policy(repo.connection, agent['id']),
                'is_controller': agent['kind'] == 'controller', 'reserve': reserve_policy(repo.connection, agent['id']),
                'models': available_models(get_settings(request).data_dir, agent.get('device_id'),
                                           db=repo.connection, project_id=project['id'])}

    @app.get('/api/projects/{slug}/settings')
    def read_settings(request: Request, slug: str):
        return project_data(*context(request, slug))

    @app.post('/api/projects/{slug}/settings/members')
    async def add_member(request: Request, slug: str):
        authorize_write(request, csrf=False)
        data = await object_body(request)
        user = authorize_write(request, data)
        _, repo, project = context(request, slug)
        device_id = positive(data.get('device_id'))
        if device_id is None:
            raise HTTPException(422, '请选择执行设备')
        device = require_device(repo, device_id)
        if device.get('server_ai_provider'):
            raise HTTPException(422, '请在模型服务中管理模型接口')
        permission = data.get('permission', 'write')
        if not isinstance(permission, str) or permission not in {'read', 'write', 'admin'}:
            raise HTTPException(422, '设备权限无效')
        repo.add_project_member(project_id=project['id'], device_id=device_id,
                                permission=permission, added_by_user_id=user['id'])
        return project_data(user, repo, project)

    @app.post('/api/projects/{slug}/settings/primary-device')
    async def set_primary(request: Request, slug: str):
        authorize_write(request, csrf=False)
        data = await object_body(request)
        user = authorize_write(request, data)
        _, repo, project = context(request, slug)
        device_id = positive(data.get('device_id'))
        if device_id is not None:
            member = repo.get_project_membership(project['id'], device_id)
            if not member or member.get('server_ai_provider') or member.get('agent_tasks_only'):
                raise HTTPException(422, '主设备必须是本项目的普通执行设备')
            if member['permission'] not in {'write', 'admin'}:
                raise HTTPException(422, '主设备需要执行与写入权限')
        previous = repo.get_project_primary_device(project['id'])
        try:
            project = repo.set_project_primary_device(project['id'], device_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        current = repo.get_project_primary_device(project['id'])
        if (previous or {}).get('id') != (current or {}).get('id'):
            repo.create_entry(project_id=project['id'], author_user_id=user['id'], source_label='Codex Hub', kind='system',
                              body_markdown=f"主设备已从 `{(previous or {}).get('label') or '未设置'}` 切换为 `{(current or {}).get('label') or '未设置'}`。已开始的任务保持原执行安排。")
        return project_data(user, repo, project)

    @app.delete('/api/projects/{slug}/settings/members/{device_id}')
    def remove_member(request: Request, slug: str, device_id: int):
        user = authorize_write(request)
        _, repo, project = context(request, slug)
        if not repo.get_project_membership(project['id'], device_id):
            raise HTTPException(404, '项目中没有此执行设备')
        repo.remove_project_member(project['id'], device_id)
        return project_data(user, repo, project)

    @app.get('/api/projects/{slug}/settings/agents/{agent_id}/model-policy')
    def read_policy(request: Request, slug: str, agent_id: int):
        _, repo, project = context(request, slug)
        return policy_data(request, repo, project, get_agent(repo, project, agent_id))

    @app.post('/api/projects/{slug}/settings/agents/{agent_id}/model-policy')
    async def write_policy(request: Request, slug: str, agent_id: int):
        authorize_write(request, csrf=False)
        data = await object_body(request)
        authorize_write(request, data)
        _, repo, project = context(request, slug)
        agent = get_agent(repo, project, agent_id)
        for key in ('mode', 'model', 'reasoning_effort', 'reason', 'reserve'):
            if key in data and not isinstance(data[key], str):
                raise HTTPException(422, '模型设置格式无效')
        mode = data.get('mode', 'controller')
        try:
            save_policy(repo.connection, agent, get_settings(request).data_dir, mode=mode,
                        model=data.get('model', '') if mode == 'manual' else '',
                        reasoning_effort=data.get('reasoning_effort', ''),
                        reason=data.get('reason', '')[:600] if mode == 'manual' else '', actor='user',
                        reserve=data.get('reserve'))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return policy_data(request, repo, project, agent)
