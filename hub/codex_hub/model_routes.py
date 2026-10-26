"""User overrides and controller-authenticated model allocation."""
from fastapi import Form, HTTPException, Request

from .agents import AgentStore
from .execution_leases import ExecutionConflict, require_execution_fence
from .model_policy import agent_policy, available_models, save_policy, task_choice, select_model, freeze_choice


def register_model_routes(app):
    from .main import (get_repo, get_settings, require_user, require_project, require_api_device,
                       require_device_write, require_project_task, json_object, redirect_to, verify_csrf_token,
                       render, annotate_membership_presence)

    def planning_task(request, slug, task_id):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_write(repo, project, device)
        task = require_project_task(repo, project, task_id)
        if not task.get('assigned_agent_id') or task['assigned_device_id'] != device['id']:
            raise HTTPException(403, '此任务不属于当前执行连接')
        agent = AgentStore(repo.connection).get(project['id'], task['assigned_agent_id'])
        agent['device_id'] = task['assigned_device_id']
        return repo, project, task, agent

    @app.get('/api/projects/{slug}/tasks/{task_id}/model-plan')
    def model_plan(request: Request, slug: str, task_id: int):
        repo, project, task, agent = planning_task(request, slug, task_id)
        selection = task_choice(repo.connection, task_id)
        if selection is None and (task['status'] not in {'open', 'claimed'} or not agent['active']):
            raise HTTPException(409, '任务已结束、已开始或智能体暂停，不再启动模型规划')
        policy = agent_policy(repo.connection, agent['id'])
        models = available_models(get_settings(request).data_dir, task['assigned_device_id'])
        if selection is None and (policy['mode'] == 'manual' or not models):
            try:
                selection = select_model(repo.connection, agent, get_settings(request).data_dir)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            with repo.connection:
                selection = freeze_choice(repo.connection, task_id, selection)
        controller = next((a for a in AgentStore(repo.connection).list(project['id'])
                           if a['kind'] == 'controller' and a['active']), None)
        return {'selection': selection, 'models': models, 'policy': policy,
                'controller': {k: controller[k] for k in ('name', 'job')} if controller else None,
                'task': {k: task[k] for k in ('id', 'title', 'priority')},
                'task_description': task.get('description', '')[:3000],
                'executor': {k: agent[k] for k in ('name', 'job')},
                'preferences': ['GPT-5.6 high', 'GPT-6 medium', 'GPT-6 high'],
                'instruction': '逐任务评估类型、难度、风险和成本；不要给同一身份的所有任务机械地选择相同模型。'}

    @app.post('/api/projects/{slug}/tasks/{task_id}/model-plan')
    async def save_model_plan(request: Request, slug: str, task_id: int):
        repo, project, task, agent = planning_task(request, slug, task_id)
        actor_device_id = task['assigned_device_id']
        data = await json_object(request)
        try:
            repo.connection.execute('BEGIN IMMEDIATE')
            task = repo.get_task(task_id)
            if task['assigned_device_id'] != actor_device_id:
                raise ExecutionConflict('任务的执行连接已改变，请由当前连接重新安排')
            agent = AgentStore(repo.connection).get(project['id'], task['assigned_agent_id'])
            agent['device_id'] = task['assigned_device_id']
            if not agent['active']:
                raise ExecutionConflict('智能体已暂停，不能继续安排新的执行')
            existing = task_choice(repo.connection, task_id)
            if existing:
                repo.connection.commit()
                return {'selection': existing}
            from .execution_leases import latest_execution
            if task['status'] not in {'open', 'claimed'} or latest_execution(repo.connection, task_id):
                raise ExecutionConflict('已开始的任务保留原模型，不能改变本次执行')
            selection = select_model(repo.connection, agent, get_settings(request).data_dir, data)
            if selection['source'] == 'controller':
                selection['source'] = 'controller_task_plan'
            selection = freeze_choice(repo.connection, task_id, selection)
            repo.connection.commit()
        except ExecutionConflict as exc:
            repo.connection.rollback()
            raise HTTPException(409, str(exc)) from None
        except (ValueError, TypeError) as exc:
            repo.connection.rollback()
            raise HTTPException(422, str(exc)) from None
        except Exception:
            repo.connection.rollback()
            raise
        return {'selection': selection}

    @app.post('/projects/{slug}/agents/{agent_id}/model-policy')
    def set_user_model(request: Request, slug: str, agent_id: int, mode: str = Form('controller'),
                       model: str = Form(''), reasoning_effort: str = Form('medium'),
                       csrf: str = Form('', alias='_csrf')):
        user = require_user(request)
        verify_csrf_token(request, csrf)
        repo = get_repo(request)
        project = require_project(repo, slug)
        store = AgentStore(repo.connection)
        try:
            agent = store.get(project['id'], agent_id)
        except ValueError:
            raise HTTPException(404, '智能体不存在') from None
        try:
            save_policy(repo.connection, agent, get_settings(request).data_dir, mode=mode,
                        model=model if mode == 'manual' else '', reasoning_effort=reasoning_effort,
                        reason='用户手动指定' if mode == 'manual' else '', actor='user')
        except ValueError as exc:
            draft = {**agent_policy(repo.connection, agent_id), 'mode': mode,
                     'model': model[:100], 'reasoning_effort': reasoning_effort[:20]}
            response = render(request, 'agent_detail.html', user=user, project=project, agent=agent,
                model_policy=draft, model_error=str(exc),
                model_options=available_models(get_settings(request).data_dir, agent.get('device_id')),
                experiences=store.experiences(project['id'], agent_id, history=True),
                tasks=[t for t in repo.list_tasks(project['id']) if t.get('assigned_agent_id') == agent_id],
                conversations=[c for c in store.conversations(project['id']) if repo.connection.execute(
                    'SELECT 1 FROM hub_conversation_agents WHERE conversation_id=? AND agent_id=?',
                    (c['id'], agent_id)).fetchone()],
                memberships=annotate_membership_presence(repo.list_project_memberships(project['id'])))
            response.status_code = 422
            return response
        return redirect_to(request, f'/projects/{slug}/agents/{agent_id}#agent-model-settings')

    @app.get('/api/projects/{slug}/agents/{agent_id}/model-policy')
    def get_model(request: Request, slug: str, agent_id: int):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_write(repo, project, device)
        try:
            agent = AgentStore(repo.connection).get(project['id'], agent_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        return {'policy': agent_policy(repo.connection, agent_id),
                'models': available_models(get_settings(request).data_dir, agent.get('device_id'))}

    @app.post('/api/projects/{slug}/agents/{agent_id}/model-policy')
    async def set_controller_model(request: Request, slug: str, agent_id: int):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_write(repo, project, device)
        data = await json_object(request)
        try:
            repo.connection.execute('BEGIN IMMEDIATE')
            source_id = int(data.get('source_task_id', 0))
            source = require_project_task(repo, project, source_id)
            controller = AgentStore(repo.connection).get(project['id'], source.get('assigned_agent_id'))
            if controller['kind'] != 'controller' or not controller['active']:
                raise HTTPException(403, '只有执行中的总控可分配模型')
            require_execution_fence(repo.connection, source, device['id'], data.get('execution'), allow_unleased=False)
            agent = AgentStore(repo.connection).get(project['id'], agent_id)
            policy = save_policy(repo.connection, agent, get_settings(request).data_dir,
                mode='controller', model=data.get('model', ''), reasoning_effort=data.get('reasoning_effort', ''),
                reason=data.get('reason', ''), actor=f'controller-task:{source_id}')
        except ExecutionConflict as exc:
            repo.connection.rollback()
            raise HTTPException(409, str(exc)) from None
        except (ValueError, TypeError) as exc:
            repo.connection.rollback()
            raise HTTPException(422, str(exc)) from None
        except Exception:
            repo.connection.rollback()
            raise
        return {'policy': policy}
