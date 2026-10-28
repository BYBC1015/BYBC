"""Role boundaries and work ownership regressions (no external model calls)."""
import pytest

from codex_hub.agents import AgentStore
from codex_hub.agent_roles import ROLES
from codex_hub.execution_leases import ExecutionStore, ExecutionConflict
from codex_hub.work_delivery import revise_task, dependency_ready
from test_agents import token_headers
from test_execution_leases import start, RESULT


@pytest.fixture
def roles(repo):
    device = repo.create_device('Personal offline-capable executor')
    project = repo.create_project(slug='role-team', name='Specialist identities', created_by_device_id=device['id'])
    store = AgentStore(repo.connection)
    agents = {}
    for key, (name, scope) in ROLES.items():
        agent = store.create(project['id'], name, scope, kind='controller' if key == 'controller' else 'worker', device_id=device['id'])
        agents[key] = store.update(project['id'], agent['id'], job=scope, instructions=scope, device_id=device['id'], active=True, role_key=key)
    source = repo.create_task(project['id'], '分配工作', assigned_agent_id=agents['controller']['id'])
    source = store.route(source['id'], {'action':'new','title':'Independent deliverables','reason':'One objective'}, 'codex_cli')
    data = {'device':device,'project':project,'task':source,'agents':agents,'store':store}
    data['run'] = start(ExecutionStore(repo), data)
    return data


def delegate(repo, d, **overrides):
    args = dict(agent_ids=[None], title='交付指定封面', description='使用本任务素材，交付可预览封面',
                work_type='visual_production',work_key='cover-product-v1',request_key='delegate-request-001',
                conversation_id=d['task']['conversation_id'],source_task_id=d['task']['id'],
                device_id=d['device']['id'],execution=d['run'])
    args.update(overrides)
    return d['store'].dispatch(repo,d['project']['id'],**args)


@pytest.mark.parametrize('role', [key for key in ROLES if key != 'controller'])
def test_task_type_selects_single_owner_without_model_cost(repo, roles, role):
    d=roles
    result=delegate(repo,d,work_type=role,fallback_reason='这是资料翻译交付，现有专业职责均不适用' if role=='general_support' else '')
    assert result['created'] and len(result['tasks'])==1
    assert result['tasks'][0]['assigned_agent_id']==d['agents'][role]['id']
    assert len(d['store'].context(d['task'])['work_assignments'])==1


def test_wrong_role_and_unsupported_fallback_are_rejected_without_tasks(repo,roles):
    d=roles; before=len(repo.list_tasks(d['project']['id']))
    with pytest.raises(ValueError,match='负责人'):
        delegate(repo,d,agent_ids=[d['agents']['engineering']['id']])
    with pytest.raises(ValueError,match='fallback_reason'):
        delegate(repo,d,work_type='general_support')
    with pytest.raises(ValueError,match='work_type'):
        delegate(repo,d,work_type='unknown')
    with pytest.raises(ValueError,match='work_key'):
        delegate(repo,d,work_key='')
    with pytest.raises(ValueError,match='fallback_reason'):
        delegate(repo,d,fallback_reason=None)
    assert len(repo.list_tasks(d['project']['id']))==before


@pytest.mark.parametrize('role', ['visual_production', 'reference_research'])
def test_paused_or_unbound_specialist_never_becomes_firefighter(repo,roles,role):
    d=roles; agent=d['agents'][role]
    for active,device in [(False,d['device']['id']),(True,None)]:
        d['store'].update(d['project']['id'],agent['id'],job=agent['job'],instructions='',device_id=device,active=active)
        with pytest.raises(ValueError,match='不能改派救火队员'):
            delegate(repo,d,work_type=role)
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_work_assignments').fetchone()[0]==0


@pytest.mark.parametrize('role', ['visual_production', 'reference_research'])
def test_duplicate_work_survives_new_request_key_and_rejects_owner_or_content_change(repo,roles,role):
    d=roles; original=delegate(repo,d,work_type=role)['tasks'][0]
    replay=delegate(repo,d,work_type=role,request_key='new-request-number',work_key='another-cover-key')
    assert not replay['created'] and replay['tasks'][0]['id']==original['id']
    with pytest.raises(ValueError,match='已有负责人'):
        delegate(repo,d,request_key='third-request-number',work_type='engineering')
    with pytest.raises(ValueError,match='revise'):
        delegate(repo,d,work_type=role,request_key='fourth-request-number',description='另一个版本要求')
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_work_assignments').fetchone()[0]==1
    from codex_hub.agent_roles import contract_hash
    assert contract_hash('Code','if ready:\n  run()',[]) != contract_hash('Code','if ready:\nrun()',[])


@pytest.mark.parametrize('role', ['visual_production', 'reference_research'])
def test_work_key_cannot_be_reset_by_opening_another_conversation(repo,roles,role):
    d=roles; delegate(repo,d,work_type=role)
    other=repo.create_task(d['project']['id'],'新对话继续',assigned_agent_id=d['agents']['controller']['id'])
    other=d['store'].route(other['id'],{'action':'new','title':'Another conversation','reason':'New place'},'codex_cli')
    newer={**d,'task':other};newer['run']=start(ExecutionStore(repo),newer,'another-controller-run')
    with pytest.raises(ValueError,match='新对话重复派工'):
        delegate(repo,newer,work_type=role,request_key='another-room-request')


def test_duplicate_retry_still_requires_current_execution(repo,roles):
    d=roles; delegate(repo,d)
    with pytest.raises(ExecutionConflict):
        delegate(repo,d,execution={**d['run'],'run_key':'expired-run-key-001'})
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_work_assignments').fetchone()[0]==1


def test_multiselect_creates_one_controller_intake_instead_of_duplicate_work(repo,roles):
    d=roles
    ids=[d['agents'][r]['id'] for r in ('visual_production','art_direction','engineering')]
    args=dict(agent_ids=ids,title='一起改网页',description='请各自分工',request_key='web-multi-request')
    result=d['store'].dispatch(repo,d['project']['id'],**args)
    assert len(result['tasks'])==1 and result['tasks'][0]['assigned_agent_id']==d['agents']['controller']['id']
    assert '总控分工' in result['tasks'][0]['route_reason']
    assert not d['store'].dispatch(repo,d['project']['id'],**args)['created']


@pytest.mark.parametrize('role', ['engineering', 'reference_research'])
def test_generic_api_cannot_bypass_managed_delegation_but_auto_role_api_works(client,repo,roles,role):
    d=roles;headers=token_headers(repo,d['device']);path='/api/projects/role-team/tasks'
    invalid=client.post(path,headers=headers,json={'title':'Bypass','assigned_agent_id':d['agents'][role]['id']})
    assert invalid.status_code==422 and 'delegate' in invalid.json()['detail']
    result=client.post(path+f"/{d['task']['id']}/delegate",headers=headers,json={
        'title':'具体交付','description':'明确验收','work_type':role,'work_key':'hub-fix-001',
        'request_key':'auto-role-request','execution':d['run']})
    assert result.status_code==201, result.text
    assert result.json()['created'] and result.json()['task']['assigned_agent_id']==d['agents'][role]['id']
    assert role in d['store'].context(d['task'])['role_contracts']


@pytest.mark.parametrize('role', ['engineering', 'reference_research'])
def test_same_role_cannot_be_registered_twice(repo,roles,role):
    d=roles;agent=d['agents']['general_support']
    with pytest.raises(ValueError):
        d['store'].update(d['project']['id'],agent['id'],job='Test',instructions='',device_id=d['device']['id'],active=True,role_key=role)
    assert d['store'].get(d['project']['id'],agent['id'])['role_key']=='general_support'


def test_reference_research_keeps_owner_for_independent_parallel_deliveries(repo,roles):
    d=roles
    first=delegate(repo,d,work_type='reference_research',title='查找聊天界面参考',
                   description='研究最近一年桌面聊天布局',work_key='refs-chat-layout')['tasks'][0]
    run=start(ExecutionStore(repo),{**d,'task':first},'reference-research-running')
    second=delegate(repo,d,work_type='reference_research',title='查找视觉短片参考',
                    description='研究最近一年城市短片画面',work_key='refs-city-film',
                    request_key='independent-reference-request')['tasks'][0]
    assert first['id']!=second['id']
    assert first['assigned_agent_id']==second['assigned_agent_id']==d['agents']['reference_research']['id']
    assert repo.get_task(first['id'])['status']=='in_progress'
    assert run['run_key']=='reference-research-running'
    assert all(row['work_type']=='reference_research' for row in d['store'].context(d['task'])['work_assignments'])


@pytest.mark.parametrize('other', ['art_direction','visual_production','postproduction','engineering','general_support'])
def test_reference_research_cannot_be_explicitly_assigned_to_another_specialist(repo,roles,other):
    d=roles
    with pytest.raises(ValueError,match='负责人'):
        delegate(repo,d,work_type='reference_research',agent_ids=[d['agents'][other]['id']])
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_work_assignments').fetchone()[0]==0


def test_reference_research_role_survives_web_identity_settings(client,repo,roles):
    from test_workspace_shell import login
    d=roles;csrf=login(client);agent=d['agents']['reference_research']
    path=f"/projects/role-team/agents/{agent['id']}"
    page=client.get(path)
    assert page.status_code==200
    assert '<option value="reference_research" selected>参考研究</option>' in page.text
    response=client.post(path,data={'_csrf':csrf,'job':agent['job'],
        'instructions':'优先最近一年案例，保留来源与真实日期',
        'device_id':str(d['device']['id']),'active':'1','role_key':'reference_research'},
        follow_redirects=False)
    assert response.status_code==303
    assert d['store'].get(d['project']['id'],agent['id'])['role_key']=='reference_research'


def test_further_revision_of_original_follows_running_continuation(repo,roles):
    d=roles;task=delegate(repo,d)['tasks'][0];executions=ExecutionStore(repo)
    run=start(executions,{**d,'task':task},'initial-worker-run')
    payload={'target_task_id':task['id'],'instruction':'先调整字体','request_key':'revision-first','execution':d['run']}
    first=revise_task(repo,d['task'],d['device']['id'],payload)
    executions.complete(task['id'],d['device']['id'],run['generation'],run['run_key'],**RESULT)
    continuation=repo.get_task(first['next_task_id'])
    start(executions,{**d,'task':continuation},'continuation-running')
    second=revise_task(repo,d['task'],d['device']['id'],{**payload,'request_key':'revision-second','instruction':'再调整颜色'})
    next_task=repo.get_task(second['next_task_id'])
    assert next_task['wait_for_task_id']==continuation['id']
    assert not dependency_ready(repo.connection,next_task)
    third=revise_task(repo,d['task'],d['device']['id'],{**payload,'request_key':'revision-third','instruction':'保留字体'})
    assert third['next_task_id']==next_task['id']
    ownership=repo.connection.execute('SELECT * FROM hub_work_assignments').fetchone()
    assert ownership['task_id']==next_task['id'] and ownership['agent_id']==task['assigned_agent_id']


def test_concurrent_retries_with_different_keys_make_one_delivery(repo,settings,roles):
    from concurrent.futures import ThreadPoolExecutor
    from codex_hub.db import connect_database
    from codex_hub.repository import Repository
    import threading
    barrier=threading.Barrier(2)
    def submit(index):
        db=connect_database(settings.database_path)
        try:
            local=Repository(db)
            barrier.wait(timeout=5)
            return delegate(local,{**roles,'store':AgentStore(db)},request_key=f'concurrent-request-{index}')
        finally:
            db.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(submit,range(2)))
    assert sum(r['created'] for r in results)==1
    assert len({r['tasks'][0]['id'] for r in results})==1


def test_web_multiselect_upload_retry_preserves_one_message_and_file(client,repo,roles):
    from test_workspace_shell import login
    d=roles;csrf=login(client)
    data={'_csrf':csrf,'agent_ids':[str(d['agents'][key]['id']) for key in ('visual_production','engineering')],
          'description':'依据附件分工制作','request_key':'role-web-upload-001'}
    files=[('attachments',('brief.txt',b'one shared brief','text/plain'))]
    response=client.post('/projects/role-team/workspace-dispatch',data=data,files=files,follow_redirects=False)
    assert response.status_code==303
    assert client.post('/projects/role-team/workspace-dispatch',data=data,files=files,follow_redirects=False).status_code==303
    tasks=[t for t in repo.list_tasks(d['project']['id']) if t['id']!=d['task']['id']]
    assert len(tasks)==1 and tasks[0]['assigned_agent_id']==d['agents']['controller']['id']
    assert len(repo.list_task_attachments(d['project']['id'],tasks[0]['id']))==1
    assert repo.connection.execute('SELECT COUNT(*) FROM attachments').fetchone()[0]==1
    assert repo.connection.execute("SELECT COUNT(*) FROM entries WHERE body_markdown='依据附件分工制作'").fetchone()[0]==1
    page=client.get(response.headers['location'])
    assert page.status_code==200 and '多选由总控拆分交付' in page.text and 'brief.txt' in page.text
