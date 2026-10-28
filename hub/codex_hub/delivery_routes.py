from __future__ import annotations
import json
import re
from pathlib import Path
from fastapi import HTTPException, Request
from .execution_leases import ExecutionStore, ExecutionConflict
from .work_delivery import artifact_links, revise_task
from .agents import now


def register_delivery_routes(app):
    from .main import (get_repo,get_settings,require_api_device,require_project,require_device_write,
        require_project_task,store_attachment_file,remove_stored_attachment,json_object,app_path,
        require_user,require_project_attachment,render)

    @app.post('/api/projects/{slug}/tasks/{task_id}/revise')
    async def revise(request:Request,slug:str,task_id:int):
        repo=get_repo(request); project=require_project(repo,slug); device=require_api_device(request)
        require_device_write(repo,project,device)
        source=require_project_task(repo,project,task_id)
        try: return {'amendment':revise_task(repo,source,device['id'],await json_object(request))}
        except ExecutionConflict as exc: raise HTTPException(409,str(exc)) from None
        except (ValueError,TypeError) as exc: raise HTTPException(422,str(exc)) from None

    @app.post('/api/projects/{slug}/tasks/{task_id}/artifacts')
    async def artifact(request:Request,slug:str,task_id:int):
        repo=get_repo(request); db=repo.connection; project=require_project(repo,slug); device=require_api_device(request)
        require_device_write(repo,project,device)
        require_project_task(repo,project,task_id)
        async with request.form(max_files=1,max_fields=6) as form:
            file=form.get('file'); key=str(form.get('artifact_key','')); reference=str(form.get('local_reference',''))
            try: execution=json.loads(str(form.get('execution','')))
            except ValueError: raise HTTPException(422,'缺少执行记录')
            if not isinstance(execution,dict) or not re.fullmatch(r'[a-f0-9]{64}',key) or not 1<=len(reference)<=1200 or not hasattr(file,'read'):
                raise HTTPException(422,'成果格式无效')
            store=ExecutionStore(repo)
            def check():
                task,record=store._fenced(task_id,device['id'],execution.get('generation'),execution.get('run_key'))
                if task['status']=='cancelled' or record['state'] not in {'running','uncertain','completed'}:
                    raise ExecutionConflict('此执行不能继续提交成果')
                if record['state']!='completed' and record['task_version']!=task['status_version']:
                    raise ExecutionConflict('任务状态已改变')
                return record
            try: check()
            except ExecutionConflict as exc: raise HTTPException(409,str(exc)) from None
            stored=await store_attachment_file(request,project,file)
            keep=False
            try:
                with db:
                    db.execute('BEGIN IMMEDIATE'); record=check()
                    old=db.execute('SELECT a.* FROM hub_task_artifacts ar JOIN attachments a ON a.id=ar.attachment_id WHERE ar.task_id=? AND ar.generation=? AND ar.artifact_key=?',
                        (task_id,record['generation'],key)).fetchone()
                    if old:
                        if old['sha256']!=stored['sha256']: raise ValueError('同一成果编号内容不一致')
                        attachment_id=old['id']
                    else:
                        count,total=db.execute('SELECT COUNT(*),COALESCE(SUM(a.byte_size),0) FROM hub_task_artifacts ar JOIN attachments a ON a.id=ar.attachment_id WHERE ar.task_id=? AND ar.generation=?',(task_id,record['generation'])).fetchone()
                        if count>=12 or total+stored['byte_size']>100*1024*1024: raise ValueError('每次执行最多12个成果，总量不超过100MB')
                        attachment_id=db.execute('''INSERT INTO attachments(project_id,entry_id,original_filename,stored_relpath,content_type,byte_size,sha256,created_by_device_id,created_at)
                            VALUES (?,?,?,?,?,?,?,?,?)''',(project['id'],record['result_entry_id'],stored['original_filename'],stored['stored_relpath'],stored['content_type'],stored['byte_size'],stored['sha256'],device['id'],now())).lastrowid
                        db.execute('INSERT INTO hub_task_artifacts VALUES (?,?,?,?,?)',(task_id,record['generation'],key,reference,attachment_id))
                        db.execute('INSERT OR IGNORE INTO hub_task_attachments VALUES (?,?,NULL)',(task_id,attachment_id))
                    if record['result_entry_id']:
                        from .revision_inputs import refresh_waiting_revisions
                        refresh_waiting_revisions(repo,task_id)
                        prefix=app_path(request,f'/projects/{slug}/attachments')
                        entry=db.execute('SELECT body_markdown FROM entries WHERE id=?',(record['result_entry_id'],)).fetchone()
                        db.execute('UPDATE entries SET body_markdown=?,updated_at=? WHERE id=?',
                            (artifact_links(db,task_id,record['generation'],entry['body_markdown'],prefix),now(),record['result_entry_id']))
                        task_summary=db.execute('SELECT result_summary FROM tasks WHERE id=?',(task_id,)).fetchone()[0]
                        db.execute('UPDATE tasks SET result_summary=?,updated_at=? WHERE id=?',
                            (artifact_links(db,task_id,record['generation'],task_summary,prefix),now(),task_id))
                        from .task_board import archive_completed_delivery
                        archive_completed_delivery(repo,task_id,get_settings(request).files_dir)
                        from .completion_followups import safely_queue_completion_followup
                        safely_queue_completion_followup(repo,task_id)
                    # Mark the stored file retained only after commit succeeds.
                keep=not bool(old)
                return {'attachment_id':attachment_id,'url':app_path(request,f'/projects/{slug}/attachments/{attachment_id}'),'replayed':bool(old)}
            except ExecutionConflict as exc: raise HTTPException(409,str(exc)) from None
            except (ValueError,TypeError) as exc: raise HTTPException(422,str(exc)) from None
            finally:
                if not keep: remove_stored_attachment(request,stored)

    @app.get('/projects/{slug}/attachments/{attachment_id}/preview')
    def preview(request:Request,slug:str,attachment_id:int):
        user=require_user(request); repo=get_repo(request); project=require_project(repo,slug)
        attachment=require_project_attachment(repo,project,attachment_id)
        root=get_settings(request).files_dir.resolve(); path=(root/attachment['stored_relpath']).resolve()
        if not path.is_relative_to(root) or not path.is_file(): raise HTTPException(404,'文件不存在')
        if path.suffix.lower()=='.pdf':
            from starlette.responses import FileResponse
            return FileResponse(path,media_type='application/pdf',filename=attachment['original_filename'],content_disposition_type='inline')
        if path.suffix.lower() not in {'.md','.txt','.csv','.json','.log'} or path.stat().st_size>1024*1024:
            raise HTTPException(415,'请下载此文件查看')
        return render(request,'artifact_preview.html',user=user,project=project,attachment=attachment,
            content=path.read_text(encoding='utf-8-sig',errors='replace'),download_url=app_path(request,f'/projects/{slug}/attachments/{attachment_id}'))
