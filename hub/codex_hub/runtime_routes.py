"""Authenticated, generation-fenced lifecycle for durable agent executions."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from .execution_leases import ExecutionConflict, ExecutionStore


def register_execution_routes(app: FastAPI) -> None:
    from .main import (get_repo, require_api_device, require_project, require_project_task,
                       require_device_write, require_device_read, json_object, require_user, get_settings)
    from .execution_progress import (initialize_progress_schema,write_progress,StreamLimiter,
                                     workspace_event_stream)
    initialize_progress_schema(app.state.repo.connection)
    app.state.workspace_stream_limiter=StreamLimiter()

    def execution(request, slug, task_id):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_write(repo, project, device)
        require_project_task(repo, project, task_id)
        return ExecutionStore(repo,files_root=get_settings(request).files_dir), device['id']

    def checked(call):
        try:
            return call()
        except ExecutionConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get('/api/projects/{slug}/execution-timings')
    def execution_timings(request: Request, slug: str, limit: int = 100):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_read(repo, project, device)
        from .execution_telemetry import execution_latency
        return {'timings': execution_latency(repo.connection, project['id'], limit=limit),
                'measurement_note': '时间来自服务器收到的公开答复；不含网页最终绘制耗时。旧记录或中途开始采样的记录不推算首条回复时间。'}

    @app.get('/api/projects/{slug}/execution-usage')
    def execution_usage(request: Request, slug: str, limit: int = 100):
        device=require_api_device(request)
        repo=get_repo(request)
        project=require_project(repo,slug)
        require_device_read(repo,project,device)
        from .execution_usage import usage_report
        return usage_report(repo.connection,project['id'],limit=limit)

    @app.post('/api/projects/{slug}/tasks/{task_id}/execution/acquire')
    async def acquire(request: Request, slug: str, task_id: int):
        store, device_id = execution(request, slug, task_id)
        data = await json_object(request)
        from .model_policy import ensure_task_choice
        checked(lambda: ensure_task_choice(store.repo, store._task(task_id, device_id), request.app.state.settings.data_dir))
        return checked(lambda: store.acquire(task_id, device_id, data.get('run_key')))

    @app.post('/api/projects/{slug}/tasks/{task_id}/execution/start')
    async def start(request: Request, slug: str, task_id: int):
        store, device_id = execution(request, slug, task_id)
        data = await json_object(request)
        return checked(lambda: store.start(task_id, device_id, data.get('generation'), data.get('run_key')))

    @app.post('/api/projects/{slug}/tasks/{task_id}/execution/renew')
    async def renew(request: Request, slug: str, task_id: int):
        store, device_id = execution(request, slug, task_id)
        data = await json_object(request)
        return checked(lambda: store.renew(task_id, device_id, data.get('generation'), data.get('run_key')))

    @app.post('/api/projects/{slug}/tasks/{task_id}/execution/progress')
    async def progress(request:Request,slug:str,task_id:int):
        store,device_id=execution(request,slug,task_id)
        data=await json_object(request)
        return checked(lambda:write_progress(store,task_id,device_id,data))

    @app.get('/projects/{slug}/events')
    async def events(request:Request,slug:str):
        user=require_user(request)
        # Web project access currently belongs to the authenticated owner;
        # device tokens retain their separate project membership checks.
        if user.get('role')!='owner':raise HTTPException(403,'project access required')
        project=require_project(get_repo(request),slug)
        release=app.state.workspace_stream_limiter.acquire(user['id'],project['id'])
        if release is None:raise HTTPException(429,'too many live workspace views',headers={'Retry-After':'3'})
        settings=get_settings(request)
        baseline=request.headers.get('last-event-id') or request.query_params.get('version')
        if baseline and (len(baseline)>80 or not all(c.isalnum() or c=='.' for c in baseline)):
            baseline=None
        stream=workspace_event_stream(request,database_path=settings.database_path,
            session_token=request.cookies.get(settings.session_cookie_name,''),user_id=user['id'],
            project_id=project['id'],release=release,baseline=baseline)
        return StreamingResponse(stream,media_type='text/event-stream',
            headers={'Cache-Control':'no-cache, no-store','X-Accel-Buffering':'no'},background=BackgroundTask(release))

    @app.post('/api/projects/{slug}/tasks/{task_id}/execution/reconcile')
    def reconcile(request: Request, slug: str, task_id: int):
        store, device_id = execution(request, slug, task_id)
        return checked(lambda: store.reconcile(task_id, device_id))

    @app.post('/api/projects/{slug}/tasks/{task_id}/execution/stopped')
    async def stopped(request: Request, slug: str, task_id: int):
        store,device_id=execution(request,slug,task_id)
        data=await json_object(request)
        return checked(lambda:store.acknowledge_stopped(task_id,device_id,data.get('generation'),data.get('run_key')))

    @app.post('/api/projects/{slug}/tasks/{task_id}/execution/complete')
    async def complete(request: Request, slug: str, task_id: int):
        store, device_id = execution(request, slug, task_id)
        data = await json_object(request)
        return checked(lambda: store.complete(task_id, device_id, data.get('generation'), data.get('run_key'),
            status=data.get('status'), summary=data.get('summary'), body=data.get('body'), reflection=data.get('reflection'),
            token_usage=data.get('token_usage')))
