"""Feedback UI and scoped engineering-maintenance endpoints."""
from __future__ import annotations

import json
import uuid

from fastapi import HTTPException, Request

from .agents import AgentStore
from .execution_leases import ExecutionConflict
from .maintenance import MaintenanceStore, capture_context, optional_id, STATUS_NAMES, TASK_NAMES


def register_maintenance_routes(app):
    from .main import (get_repo, get_settings, require_user, require_project, require_api_device,
                       require_device_read, require_device_write, verify_csrf_token, render, redirect_to, json_object)

    def context(request, slug):
        repo = get_repo(request)
        project = require_project(repo,slug)
        return repo,project,MaintenanceStore(repo)

    def can_manage(repo,project,device):
        member = repo.get_project_membership(project['id'],device['id'])
        return project.get('primary_device_id')==device['id'] or bool(member and member['permission']=='admin')

    def feedback_for_device(repo,project,device,feedback_id):
        require_device_read(repo,project,device)
        feedback = repo.connection.execute('SELECT * FROM hub_feedback WHERE project_id=? AND id=?',
                                            (project['id'],feedback_id)).fetchone()
        if not feedback:
            raise HTTPException(404,'反馈不存在')
        linked = repo.connection.execute('''SELECT 1 FROM hub_maintenance_feedback f
            JOIN hub_maintenance_runs r ON r.id=f.run_id JOIN tasks t ON t.id=r.task_id
            WHERE f.feedback_id=? AND r.project_id=? AND t.assigned_device_id=?''',
            (feedback_id,project['id'],device['id'])).fetchone()
        if not can_manage(repo,project,device) and not linked:
            raise HTTPException(403,'此反馈未分配给当前执行连接')
        return dict(feedback)

    def view(request,user,repo,project,store,*,mode='feedback',draft=None,error='',status=200):
        content = render(request,'feedback.html',user=user,project=project,mode=mode,
            can_configure=user.get('role')=='owner',
            feedbacks=store.feedback(project['id'],user['id']),maintenance=store.status(project['id']),
            engineers=[a for a in AgentStore(repo.connection).list(project['id']) if a['kind']=='worker'],
            draft=draft or {},error=error,status_names=STATUS_NAMES,task_names=TASK_NAMES)
        content.status_code=status
        return content

    @app.get('/projects/{slug}/feedback')
    def feedback_page(request:Request,slug:str):
        user=require_user(request)
        repo,project,store=context(request,slug)
        draft={name:request.query_params.get(name,'') for name in ('kind','conversation_id','task_id','agent_id','page_url')}
        draft['kind']='idea' if draft['kind']=='idea' else 'bug'
        draft['request_key']=uuid.uuid4().hex
        return view(request,user,repo,project,store,draft=draft)

    @app.post('/projects/{slug}/feedback')
    async def submit_feedback(request:Request,slug:str):
        user=require_user(request)
        repo,project,store=context(request,slug)
        async with request.form(max_files=0,max_fields=30) as form:
            verify_csrf_token(request,str(form.get('_csrf','')))
            draft={key:str(value) for key,value in form.items() if key!='_csrf'}
            try:
                report_context=capture_context(repo,project['id'],request,draft,get_settings(request).data_dir)
                feedback=store.create_feedback(project['id'],user['id'],kind=draft.get('kind','bug'),
                    title=draft.get('title',''),body=draft.get('body',''),reproduction=draft.get('reproduction',''),
                    expected=draft.get('expected',''),context=report_context,request_key=draft.get('request_key',''))
            except (ValueError,TypeError) as exc:
                return view(request,user,repo,project,store,draft=draft,error=str(exc),status=422)
        return redirect_to(request,f"/projects/{slug}/feedback#feedback-{feedback['id']}")

    @app.post('/projects/{slug}/feedback/{feedback_id}/status')
    async def update_feedback(request:Request,slug:str,feedback_id:int):
        user=require_user(request)
        repo,project,store=context(request,slug)
        async with request.form(max_files=0,max_fields=6) as form:
            verify_csrf_token(request,str(form.get('_csrf','')))
            try:
                store.set_status(project['id'],feedback_id,str(form.get('status','')),str(form.get('note','')),user_id=user['id'],
                                 evidence=str(form.get('verification_note','')))
            except ValueError as exc:
                raise HTTPException(422,str(exc)) from exc
        return redirect_to(request,f'/projects/{slug}/feedback#feedback-{feedback_id}')

    @app.get('/projects/{slug}/maintenance')
    def maintenance_page(request:Request,slug:str):
        user=require_user(request)
        repo,project,store=context(request,slug)
        return view(request,user,repo,project,store,mode='maintenance')

    @app.post('/projects/{slug}/maintenance/config')
    async def maintenance_config(request:Request,slug:str):
        user=require_user(request)
        if user.get('role')!='owner':
            raise HTTPException(403,'只有工作区管理员可以调整维护安排')
        repo,project,store=context(request,slug)
        async with request.form(max_files=0,max_fields=6) as form:
            verify_csrf_token(request,str(form.get('_csrf','')))
            try:
                store.configure(project['id'],optional_id(form.get('agent_id')),form.get('enabled')=='1',user['id'])
            except ValueError as exc:
                return view(request,user,repo,project,store,mode='maintenance',error=str(exc),status=422)
        return redirect_to(request,f'/projects/{slug}/maintenance')

    @app.post('/projects/{slug}/maintenance/queue')
    async def maintenance_queue(request:Request,slug:str):
        user=require_user(request)
        if user.get('role')!='owner':
            raise HTTPException(403,'只有工作区管理员可以安排维护工作')
        repo,project,store=context(request,slug)
        async with request.form(max_files=0,max_fields=3) as form:
            verify_csrf_token(request,str(form.get('_csrf','')))
            try:
                store.queue_week(project['id'])
            except ValueError as exc:
                return view(request,user,repo,project,store,mode='maintenance',error=str(exc),status=422)
        return redirect_to(request,f'/projects/{slug}/maintenance')

    @app.get('/api/projects/{slug}/maintenance')
    def maintenance_api(request:Request,slug:str):
        device=require_api_device(request)
        repo,project,store=context(request,slug)
        require_device_read(repo,project,device)
        return store.status(project['id'])

    @app.get('/api/projects/{slug}/maintenance/feedback/{feedback_id}')
    def maintenance_feedback_api(request:Request,slug:str,feedback_id:int):
        device=require_api_device(request)
        repo,project,store=context(request,slug)
        row=feedback_for_device(repo,project,device,feedback_id)
        return {'feedback':{key:row[key] for key in ('id','kind','title','body','reproduction','expected','status','linked_task_id','verification_note')},
                'context':json.loads(row['context_json'])}

    @app.get('/api/projects/{slug}/maintenance/tasks/{task_id}')
    def maintenance_task_context(request:Request,slug:str,task_id:int):
        device=require_api_device(request)
        repo,project,store=context(request,slug)
        require_device_read(repo,project,device)
        row=repo.connection.execute('''SELECT r.*,t.assigned_device_id FROM hub_maintenance_runs r JOIN tasks t ON t.id=r.task_id
            WHERE r.project_id=? AND t.id=? AND t.project_id=r.project_id''',(project['id'],task_id)).fetchone()
        if not row:
            raise HTTPException(404,'维护任务不存在')
        if row['assigned_device_id']!=device['id'] and not can_manage(repo,project,device):
            raise HTTPException(403,'此维护任务属于其他执行连接')
        snapshot=json.loads(row['snapshot_json'])
        records=[dict(item) for item in repo.connection.execute('''SELECT f.id,f.kind,f.title,f.status,f.linked_task_id
            FROM hub_maintenance_feedback m JOIN hub_feedback f ON f.id=m.feedback_id WHERE m.run_id=? AND f.project_id=? ORDER BY f.id''',
            (row['id'],project['id']))]
        return {'run':store.run(row['id']),'feedback':records,'diagnostics':snapshot.get('diagnostic_full',snapshot.get('diagnostic_excerpt',''))}

    @app.post('/api/projects/{slug}/maintenance/feedback/{feedback_id}/status')
    async def maintenance_feedback_status_api(request:Request,slug:str,feedback_id:int):
        device=require_api_device(request)
        repo,project,store=context(request,slug)
        require_device_write(repo,project,device)
        feedback=feedback_for_device(repo,project,device,feedback_id)
        task=repo.get_task(feedback['linked_task_id']) if feedback['linked_task_id'] else None
        if not can_manage(repo,project,device) and (not task or task['assigned_device_id']!=device['id'] or task['status']=='cancelled'):
            raise HTTPException(403,'反馈未关联当前执行连接可处理的维护任务')
        data=await json_object(request)
        try:
            store.set_status(project['id'],feedback_id,data.get('status',''),data.get('note',''),device_id=device['id'],
                             evidence=data.get('verification_note',data.get('evidence','')),task_id=data.get('task_id'),execution=data.get('execution'))
        except ExecutionConflict as exc:
            raise HTTPException(409,str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from exc
        return {'ok':True}
