"""Authenticated, generation-fenced lifecycle for durable agent executions."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request

from .execution_leases import ExecutionConflict, ExecutionStore


def register_execution_routes(app: FastAPI) -> None:
    from .main import (get_repo, require_api_device, require_project, require_project_task,
                       require_device_write, json_object)

    def execution(request, slug, task_id):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_write(repo, project, device)
        require_project_task(repo, project, task_id)
        return ExecutionStore(repo), device['id']

    def checked(call):
        try:
            return call()
        except ExecutionConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from None

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

    @app.post('/api/projects/{slug}/tasks/{task_id}/execution/reconcile')
    def reconcile(request: Request, slug: str, task_id: int):
        store, device_id = execution(request, slug, task_id)
        return checked(lambda: store.reconcile(task_id, device_id))

    @app.post('/api/projects/{slug}/tasks/{task_id}/execution/complete')
    async def complete(request: Request, slug: str, task_id: int):
        store, device_id = execution(request, slug, task_id)
        data = await json_object(request)
        return checked(lambda: store.complete(task_id, device_id, data.get('generation'), data.get('run_key'),
            status=data.get('status'), summary=data.get('summary'), body=data.get('body'), reflection=data.get('reflection')))
