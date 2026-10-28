"""Real dispatch, API and board regressions for hybrid execution surfaces."""
import pytest

from codex_hub.agents import initialize_agent_schema
from codex_hub.execution_context import execution_identity
from codex_hub.execution_leases import ExecutionStore
from codex_hub.handoffs import ask_handoff
from codex_hub.task_board import organize_task
from codex_hub.work_delivery import revise_task
from codex_hub.workspace import _task_views
from test_agent_roles import roles, delegate
from test_agents import token_headers
from test_execution_leases import start, RESULT
from test_workspace_shell import login


@pytest.mark.parametrize('role,route', [
    ('engineering','project'), ('postproduction','project'),
    ('visual_production','direct'), ('reference_research','direct'),
    ('art_direction','direct'), ('general_support','direct'),
])
def test_defaults_follow_delivery_profession_and_freeze_context(repo,roles,role,route):
    d=roles
    task=delegate(repo,d,work_type=role,
        fallback_reason='这是其他现有专业职责均不适用的独立支持交付' if role=='general_support' else '')['tasks'][0]
    assert task['execution_route']==route and task['execution_route_reason']
    assert task['assigned_agent_id']==d['agents'][role]['id']
    context=d['store'].context(task)
    work=execution_identity(context)['work']
    for key in ('execution_route','execution_route_reason','conversation_id','assigned_agent_id','assigned_device_id'):
        assert context['task'][key]==task[key] and work[key]==task[key]
    assert context['agent']['id']==task['assigned_agent_id']
    # Identity edits do not retroactively change a previously accepted route.
    repo.connection.execute("UPDATE hub_agents SET role_key='' WHERE id=?",(task['assigned_agent_id'],))
    repo.connection.commit()
    assert repo.get_task(task['id'])['execution_route']==route


def test_existing_tasks_remain_single_run_on_additive_schema_retry(repo,roles):
    task=roles['task']
    assert task['execution_route']=='direct'
    initialize_agent_schema(repo.connection)
    assert repo.get_task(task['id'])['execution_route']=='direct'


@pytest.mark.parametrize('route,mode', [('direct','shared'),('direct','isolated'),('project','shared')])
def test_delegate_api_accepts_explicit_surface_and_explains_it(client,repo,roles,route,mode):
    d=roles
    response=client.post(f"/api/projects/role-team/tasks/{d['task']['id']}/delegate",
        headers=token_headers(repo,d['device']),json={
            'title':'一次视觉交付','description':'生成一张可预览图片','work_type':'visual_production',
            'work_key':'surface-explicit-001','request_key':'surface-request-001','execution':d['run'],
            'execution_route':route,'execution_mode':mode,'execution_route_reason':'本次具体交付需要这条路线'})
    assert response.status_code==201,response.text
    task=response.json()['task']
    assert task['execution_route']==route and task['execution_mode']==mode
    assert task['execution_route_reason']=='本次具体交付需要这条路线'
    assert task['assigned_agent_id']==d['agents']['visual_production']['id']
    context=client.get(f"/api/projects/role-team/tasks/{task['id']}/agent-context",headers=token_headers(repo,d['device']))
    assert context.status_code==200
    assert context.json()['task']['execution_route']==route


@pytest.mark.parametrize('fields', [
    {'execution_route':'chat'}, {'execution_route':'unknown'}, {'execution_route':False},
    {'execution_route':['direct']}, {'execution_route':'project','execution_mode':'isolated'},
    {'execution_route_reason':None}, {'execution_route_reason':'x'*501},
])
def test_invalid_delegate_surfaces_create_no_task_or_assignment(client,repo,roles,fields):
    d=roles; before=len(repo.list_tasks(d['project']['id']))
    response=client.post(f"/api/projects/role-team/tasks/{d['task']['id']}/delegate",
        headers=token_headers(repo,d['device']),json={
            'title':'交付','description':'一个具体结果','work_type':'visual_production',
            'work_key':'surface-invalid-001','request_key':'surface-invalid-request',
            'execution':d['run'],**fields})
    assert response.status_code==422,response.text
    assert len(repo.list_tasks(d['project']['id']))==before
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_work_assignments').fetchone()[0]==0


def test_duplicate_work_key_and_content_cannot_silently_change_route(client,repo,roles):
    d=roles; headers=token_headers(repo,d['device'])
    url=f"/api/projects/role-team/tasks/{d['task']['id']}/delegate"
    payload={'title':'一次封面制作','description':'生成一张图片','work_type':'visual_production',
             'work_key':'cover-frozen-route','request_key':'route-first-request',
             'execution':d['run'],'execution_route':'direct','execution_route_reason':'单次成果'}
    first=client.post(url,headers=headers,json=payload)
    assert first.status_code==201,first.text
    assert not client.post(url,headers=headers,json=payload).json()['created']
    # Same request key cannot mutate either the route or its justification.
    for changes in ({'execution_route':'project'},{'execution_route_reason':'另一个理由'}):
        assert client.post(url,headers=headers,json={**payload,**changes}).status_code==422
    # New request keys, and even a renamed work key, cannot duplicate the same work.
    for key in ('cover-frozen-route','another-cover-work'):
        changed=client.post(url,headers=headers,json={**payload,'request_key':'route-next-request',
            'work_key':key,'execution_route':'project'})
        assert changed.status_code==422 and 'execution_route' in changed.json()['detail']
    assert repo.get_task(first.json()['task']['id'])['execution_route']=='direct'
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_work_assignments').fetchone()[0]==1


def test_controller_web_turns_stay_in_chat_without_work_board_clutter(client,repo,roles):
    d=roles;csrf=login(client)
    payload={'_csrf':csrf,'agent_ids':str(d['agents']['controller']['id']),
             'description':'能解释一下刚才的安排吗？','request_key':'controller-chat-turn-001',
             'conversation_id':str(d['task']['conversation_id'])}
    url='/projects/role-team/workspace-dispatch'
    response=client.post(url,data=payload,follow_redirects=False)
    assert response.status_code==303,response.text
    assert client.post(url,data=payload,follow_redirects=False).status_code==303
    turn=next(t for t in repo.list_tasks(d['project']['id']) if t['description']==payload['description'])
    assert turn['execution_route']=='chat'
    run=start(ExecutionStore(repo),{**d,'task':turn},'controller-chat-running')
    assert turn['id'] not in {t['id'] for t in _task_views(repo,d['project']['id'],board='active')}
    assert turn['id'] not in {t['id'] for t in _task_views(repo,d['project']['id'],board='history')}
    page=client.get(response.headers['location'])
    assert payload['description'] in page.text
    assert f'data-ws-task-card="{turn["id"]}"' not in page.text
    ExecutionStore(repo).complete(turn['id'],d['device']['id'],run['generation'],run['run_key'],
        **{**RESULT,'body':'这是本轮的直接回答，实际工作仍按原安排继续。'})
    page=client.get(response.headers['location'])
    assert '这是本轮的直接回答' in page.text
    # Explicitly exposing a record remains reversible and does not erase chat.
    organize_task(repo,d['project']['id'],turn['id'],'restore')
    assert turn['id'] in {t['id'] for t in _task_views(repo,d['project']['id'],board='active')}


def test_web_specialist_defaults_and_retry_route_are_consistent(client,repo,roles):
    d=roles;csrf=login(client);url='/projects/role-team/workspace-dispatch'
    for role,route in [('engineering','project'),('visual_production','direct')]:
        payload={'_csrf':csrf,'agent_ids':str(d['agents'][role]['id']),
            'description':'仅由本专业执行','request_key':'web-specialist-'+role}
        assert client.post(url,data=payload,follow_redirects=False).status_code==303
        assert client.post(url,data=payload,follow_redirects=False).status_code==303
        task=next(t for t in repo.list_tasks(d['project']['id']) if t['assigned_agent_id']==d['agents'][role]['id'])
        assert task['execution_route']==route
        changed=client.post(url,data={**payload,'execution_route':'chat'},follow_redirects=False)
        assert changed.status_code==200 and '同一请求标识' in changed.text
        assert repo.get_task(task['id'])['execution_route']==route


def test_project_revision_preserves_session_surface_and_handoff_is_chat(repo,roles):
    d=roles;task=delegate(repo,d,work_type='engineering')['tasks'][0]
    run=start(ExecutionStore(repo),{**d,'task':task},'project-work-running')
    revision=revise_task(repo,d['task'],d['device']['id'],{
        'target_task_id':task['id'],'instruction':'只修正当前导航按钮','request_key':'project-work-revision',
        'execution':d['run']})
    following=repo.get_task(revision['next_task_id'])
    assert following['execution_route']=='project'
    assert following['execution_route_reason']==task['execution_route_reason']
    assert following['conversation_id']==task['conversation_id']
    handoff=ask_handoff(repo,task,d['device']['id'],{
        'question':'这个按钮沿用现有图标还是新的图标？','request_key':'project-clarification',
        'execution':run})
    reply=repo.get_task(handoff['controller_task_id'])
    assert reply['execution_route']=='chat'
    assert reply['conversation_id']==task['conversation_id']
    assert reply['id'] not in {t['id'] for t in _task_views(repo,d['project']['id'],board='active')}


def test_prompt_uses_surface_without_equating_agent_and_conversation(repo,roles):
    task=delegate(repo,roles)['tasks'][0]
    prompt=roles['store'].prompt(task)
    assert '一张图片' in prompt and '图片生成和修图属于视觉制作' in prompt
    assert 'project 是需要本机文件' in prompt and 'execution_route' in prompt
    assert '同一智能体可以参与多个工作会话' in prompt
