import pytest

from codex_hub.agents import AgentStore
from codex_hub.execution_leases import ExecutionStore,ExecutionConflict
from codex_hub.handoffs import ask_handoff,answer_handoff,handoff_context
from test_execution_leases import lease_task,start,RESULT
from test_agents import token_headers
from test_workspace_shell import login


def ask(repo,d,run,**changes):
    return ask_handoff(repo,repo.get_task(d['task']['id']),d['device']['id'],
        {'execution':run,'question':'这里的标题是否需要保留，细化一下画幅要求？','request_key':'handoff-question-001',**changes})


def reply_run(repo,d,handoff):
    task=repo.get_task(handoff['controller_task_id'])
    return task,start(ExecutionStore(repo),{**d,'task':task},f'controller-answer-{handoff["id"]:08d}')


def test_real_question_and_answer_are_visible_and_resume_original_task(client,repo,lease_task):
    d=lease_task;store=ExecutionStore(repo);run=start(store,d)
    first=ask(repo,d,run)
    assert ask(repo,d,run)['id']==first['id']
    assert repo.get_task(d['task']['id'])['coordination_state']=='waiting'
    assert store.reconcile(d['task']['id'],d['device']['id'])['execution']['state']=='running'
    assert d['task']['id'] not in {t['id'] for t in repo.list_inbox_tasks_for_device(d['device']['id'])}
    reply,reply_execution=reply_run(repo,d,first)
    login(client)
    room=repo.get_task(d['task']['id'])['conversation_id']
    page=client.get(f"/projects/{d['project']['slug']}/conversations/{room}")
    assert '交接第 1 轮' in page.text and '画幅要求' in page.text
    final=store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],**RESULT)
    assert final['yielded'] and repo.get_task(d['task']['id'])['status']=='open'
    with pytest.raises(ExecutionConflict,match='交接答复'):
        store.acquire(d['task']['id'],d['device']['id'],'resume-too-early-001')
    answer_handoff(repo,reply,d['device']['id'],{'execution':reply_execution,'handoff_id':first['id'],'answer':'标题保留，按 16:9 制作。'})
    store.complete(reply['id'],d['device']['id'],reply_execution['generation'],reply_execution['run_key'],**RESULT)
    assert repo.connection.execute('SELECT COUNT(*) FROM entries WHERE task_id=?',(reply['id'],)).fetchone()[0]==1
    page=client.get(f"/projects/{d['project']['slug']}/conversations/{room}")
    assert '标题保留，按 16:9 制作。' in page.text
    resumed=store.acquire(d['task']['id'],d['device']['id'],'resume-after-answer-001')
    assert resumed['execution']['generation']==2
    assert '16:9' in AgentStore(repo.connection).prompt(repo.get_task(d['task']['id']))


def test_answer_may_arrive_before_worker_exits_without_early_restart(repo,lease_task):
    d=lease_task;store=ExecutionStore(repo);run=start(store,d);question=ask(repo,d,run)
    reply,reply_execution=reply_run(repo,d,question)
    store.complete(reply['id'],d['device']['id'],reply_execution['generation'],reply_execution['run_key'],
        **{**RESULT,'body':'保留标题，16:9 横屏。'})
    assert repo.get_task(d['task']['id'])['coordination_state']=='ready'
    with pytest.raises(ExecutionConflict): store.acquire(d['task']['id'],d['device']['id'],'cannot-start-yet-001')
    done=store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],**RESULT)
    assert done['yielded']
    assert store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],**RESULT)['replayed']
    assert store.acquire(d['task']['id'],d['device']['id'],'after-process-exit-001')['execution']['generation']==2


def test_round_limit_two_and_stale_worker_question_cannot_loop(repo,lease_task):
    d=lease_task;store=ExecutionStore(repo)
    for number in (1,2):
        run=start(store,d,f'worker-round-{number:08d}')
        question=ask(repo,d,run,request_key=f'question-round-{number:08d}',question=f'第 {number} 个细节是什么？')
        reply,reply_execution=reply_run(repo,d,question)
        store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],**RESULT)
        with pytest.raises(ExecutionConflict): ask(repo,d,run)
        store.complete(reply['id'],d['device']['id'],reply_execution['generation'],reply_execution['run_key'],**{**RESULT,'body':'按已给的范围执行。'})
    run=start(store,d,'worker-final-round-001')
    with pytest.raises(ValueError,match='轮数'): ask(repo,d,run,request_key='third-question-round')
    assert handoff_context(repo.connection,repo.get_task(d['task']['id']))['max_rounds']==2


def test_cancelled_original_cannot_be_revived_by_answer(repo,lease_task):
    d=lease_task;run=start(ExecutionStore(repo),d);question=ask(repo,d,run)
    reply,reply_execution=reply_run(repo,d,question)
    repo.update_task_status(d['task']['id'],'cancelled','用户取消')
    with pytest.raises(ExecutionConflict):
        answer_handoff(repo,reply,d['device']['id'],{'execution':reply_execution,'handoff_id':question['id'],'answer':'继续'})
    assert repo.get_task(d['task']['id'])['status']=='cancelled'


@pytest.mark.parametrize('answer_fails_first',[True,False])
def test_failed_answer_exposes_blocker_then_real_answer_resumes_same_task(repo,lease_task,answer_fails_first):
    d=lease_task;store=ExecutionStore(repo);execution=start(store,d);handoff=ask(repo,d,execution)
    reply,reply_execution=reply_run(repo,d,handoff)
    def finish_worker():
        store.complete(d['task']['id'],d['device']['id'],execution['generation'],execution['run_key'],**RESULT)
    if not answer_fails_first:finish_worker()
    store.complete(reply['id'],d['device']['id'],reply_execution['generation'],reply_execution['run_key'],
        **{**RESULT,'status':'blocked','body':'画幅信息互相冲突，暂时无法给出可靠答复。'})
    if answer_fails_first:finish_worker()
    original=repo.get_task(d['task']['id'])
    assert original['status']=='blocked' and original['coordination_state']=='ready'
    source=repo.create_task(d['project']['id'],'补充真实答复',assigned_agent_id=d['controller']['id'])
    d['agents'].route(source['id'],{'action':'reuse','conversation_id':original['conversation_id'],'reason':'明确范围'},'codex_cli')
    source=repo.get_task(source['id']);run=start(store,{**d,'task':source},'answer-after-blocked-001')
    answer_handoff(repo,source,d['device']['id'],{'execution':run,'handoff_id':handoff['id'],'answer':'以用户最新要求为准：16:9。'})
    assert repo.get_task(d['task']['id'])['status']=='open'
    assert store.acquire(d['task']['id'],d['device']['id'],'resume-after-blocked-001')['execution']['generation']==2


def test_web_round_limit_and_api_scope(client,repo,lease_task):
    d=lease_task;csrf=login(client);controller=d['controller'];slug=d['project']['slug']
    response=client.post(f'/projects/{slug}/agents/{controller["id"]}',data={'_csrf':csrf,'job':controller['job'],
        'device_id':str(d['device']['id']),'active':'1','handoff_round_limit':'4'},follow_redirects=False)
    assert response.status_code==303
    assert AgentStore(repo.connection).get(d['project']['id'],controller['id'])['handoff_round_limit']==4
    run=start(ExecutionStore(repo),d)
    response=client.post(f'/api/projects/{slug}/tasks/{d["task"]["id"]}/handoff/question',headers=token_headers(repo,d['device']),
        json={'execution':run,'question':'确认输入尺寸？','request_key':'api-question-001'})
    assert response.status_code==200,response.text


def test_worker_recovers_lost_yield_receipt_and_resumes_same_task_after_answer(repo,tmp_path,monkeypatch):
    from test_execution_recovery import runtime,restart
    monkeypatch.setattr('codex_bridge.model_allocator.allocate_task_model',lambda *args:{})
    runner,client,executor=runtime(repo,tmp_path)
    agents=AgentStore(repo.connection)
    agents.create(client.project['id'],'总控','协调',kind='controller',device_id=client.device['id'])
    task_id=client.task_id;original_run=executor.run;questions=[]
    def run(*args,execution_context=None,**kwargs):
        if not questions:
            questions.append(ask_handoff(repo,repo.get_task(task_id),client.device['id'],{
                'execution':execution_context,'question':'画幅应为多少？','request_key':'real-worker-question'}))
        return original_run(*args,execution_context=execution_context,**kwargs)
    executor.run=run
    original_inbox=client.get_inbox
    def inbox():
        data=original_inbox()
        data['tasks']=[t for t in data['tasks'] if t.get('coordination_state')!='waiting']
        return data
    client.get_inbox=inbox;client.fail_complete_ack=True
    runner.process_once()
    assert len(executor.calls)==1 and runner.state.task_deliveries
    recovered=restart(runner,client,executor);recovered.process_once()
    assert not recovered.state.task_deliveries and str(task_id) not in recovered.state.seen_task_ids
    reply=repo.get_task(questions[0]['controller_task_id']);store=ExecutionStore(repo)
    prepared=store.acquire(reply['id'],client.device['id'],'controller-real-answer')['execution']
    execution=store.start(reply['id'],client.device['id'],prepared['generation'],prepared['run_key'])['execution']
    store.complete(reply['id'],client.device['id'],execution['generation'],execution['run_key'],**{**RESULT,'body':'请用 16:9。'})
    client.fail_complete_ack=False;recovered.process_once()
    assert len(executor.calls)==2 and repo.get_task(task_id)['status']=='review'
    assert str(task_id) in recovered.state.seen_task_ids
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_task_handoffs').fetchone()[0]==1
