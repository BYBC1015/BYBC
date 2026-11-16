"""Owner configuration and device-fenced model connection access."""
import re

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .execution_leases import ExecutionConflict, require_execution_fence
from .mobile_routes import object_body
from .model_policy import task_choice
from .model_services import ModelServices


def register_model_service_routes(app):
    from .main import (get_repo, get_settings, require_project, require_user, require_api_device,
                       require_project_task, require_device_write, verify_csrf_token, annotate_membership_presence)

    def owner(request):
        user = require_user(request)
        if user.get('role') != 'owner':
            raise HTTPException(403, '只有管理员可以管理模型服务')
        return user

    async def body(request):
        owner(request)
        data = await object_body(request)
        # Reading a request may yield while another session revokes ownership.
        # Recheck permission at the mutation boundary, not only before upload.
        owner(request)
        verify_csrf_token(request, request.headers.get('x-csrf-token') or str(data.get('_csrf') or ''))
        return data

    def context(request, slug):
        repo = get_repo(request)
        project = require_project(repo, slug)
        return repo, project, ModelServices(repo.connection, get_settings(request).data_dir)

    def public(request, slug):
        user = require_user(request)
        repo, project, store = context(request, slug)
        members = annotate_membership_presence(repo.list_project_memberships(project['id']))
        devices = [{k: m.get(k) for k in ('device_id', 'device_label', 'is_agent_online')}
                   for m in members if m['permission'] in {'write', 'admin'} and not m.get('server_ai_provider')]
        services = store.list(project['id'])
        online = {m['device_id'] for m in devices if m['is_agent_online']}
        for item in services:
            if item['status'] == 'queued' and item['device_id'] not in online:
                item['status_message'] = '执行电脑离线，检查已保留，联网后继续。'
        return {'services': services, 'devices': devices, 'can_manage': user.get('role') == 'owner'}

    @app.get('/api/projects/{slug}/model-services')
    def list_services(request: Request, slug: str):
        return public(request, slug)

    @app.post('/api/projects/{slug}/model-services')
    async def add_service(request: Request, slug: str):
        data = await body(request)
        _, project, store = context(request, slug)
        try:
            service_id = store.save(project['id'], data)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {**public(request, slug), 'id': service_id}

    @app.put('/api/projects/{slug}/model-services/{service_id}')
    async def update_service(request: Request, slug: str, service_id: int):
        data = await body(request)
        _, project, store = context(request, slug)
        try:
            store.save(project['id'], data, service_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return public(request, slug)

    @app.delete('/api/projects/{slug}/model-services/{service_id}')
    def delete_service(request: Request, slug: str, service_id: int):
        owner(request)
        verify_csrf_token(request, request.headers.get('x-csrf-token', ''))
        _, project, store = context(request, slug)
        try:
            store.delete(project['id'], service_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        return public(request, slug)

    @app.post('/api/projects/{slug}/model-services/{service_id}/test')
    async def test_service(request: Request, slug: str, service_id: int):
        await body(request)
        _, project, store = context(request, slug)
        try:
            job_id = store.queue_probe(project['id'], service_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        return {'queued': True, 'job_id': job_id, 'message': '已安排执行电脑读取模型目录，不会调用付费生成。'}

    @app.post('/api/model-services/probes/claim')
    def claim_probe(request: Request):
        device = require_api_device(request)
        store = ModelServices(get_repo(request).connection, get_settings(request).data_dir)
        return JSONResponse({'job': store.claim_probe(device['id'])}, headers={'Cache-Control': 'no-store'})

    @app.post('/api/model-services/probes/{job_id}/complete')
    async def complete_probe(request: Request, job_id: int):
        require_api_device(request)
        store = ModelServices(get_repo(request).connection, get_settings(request).data_dir)
        data = await object_body(request)
        device = require_api_device(request)
        try:
            return store.complete_probe(device['id'], job_id, data)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post('/api/projects/{slug}/tasks/{task_id}/model-service')
    async def task_service(request: Request, slug: str, task_id: int):
        device = require_api_device(request)
        repo, project, store = context(request, slug)
        require_device_write(repo, project, device)
        require_project_task(repo, project, task_id)
        data = await object_body(request)
        device = require_api_device(request)
        require_device_write(repo, project, device)
        task = require_project_task(repo, project, task_id)
        try:
            # Reading its own frozen connection is allowed for read-only followups.
            require_execution_fence(repo.connection, {**task, 'controller_read_only': False}, device['id'],
                                    data.get('execution'), allow_unleased=False)
            choice = task_choice(repo.connection, task_id) or {}
            match = re.fullmatch(r'hub-service/([1-9][0-9]*)', choice.get('model', ''))
            if not match or type(choice.get('service_revision')) is not int:
                raise ValueError('任务没有已冻结的模型服务')
            service = store.version(project['id'], int(match[1]), choice['service_revision'], device['id'])
        except (ValueError, ExecutionConflict) as exc:
            raise HTTPException(409, str(exc)) from None
        return JSONResponse({'service': service}, headers={'Cache-Control': 'no-store'})
