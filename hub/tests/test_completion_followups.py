"""Result closure is durable, bounded, read-only and independent of the native UI."""
import json
import pytest

from codex_hub import completion_followups as followups
from codex_hub.execution_leases import ExecutionStore, latest_execution
from codex_hub.task_board import active_sql
from codex_hub.model_policy import select_model, save_policy
from test_execution_leases import lease_task, start, RESULT
from test_work_delivery import upload_result
from test_model_policy import setup_team
from test_agents import token_headers


def finish(repo, d, *, store=None, run=None, status='review'):
    store=store or ExecutionStore(repo)
    run=run or start(store,d,f"completion-source-{d['task']['id']:05}")
    result=store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],
                          **{**RESULT,'status':status})
    return run,result


def summaries(repo):
    return [dict(r) for r in repo.connection.execute("SELECT * FROM tasks WHERE work_stage='delivery_summary' ORDER BY id")]


def notices(repo):
    return [dict(r) for r in repo.connection.execute("SELECT * FROM entries WHERE metadata_json LIKE '%completion_notice%' ORDER BY id")]


def another(repo,d):
    task=repo.create_task(d['project']['id'],'另一项制作',description='必须保持金色主题',assigned_agent_id=d['worker']['id'])
    room=repo.get_task(d['task']['id'])['conversation_id']
    task=d['agents'].route(task['id'],{'action':'reuse','conversation_id':room,'reason':'同一个用户项目'},'codex_cli')
    return {**d,'task':task}


def test_completion_receipt_is_immediate_idempotent_hidden_and_no_summary_loop(repo,lease_task):
    d=lease_task;run,result=finish(repo,d)
    task=summaries(repo)[0]
    assert task['controller_read_only']==1 and task['execution_route']=='chat'
    assert task['assigned_agent_id']==d['controller']['id']
    assert task['conversation_id']==repo.get_task(d['task']['id'])['conversation_id']
    assert repo.connection.execute(f'SELECT COUNT(*) FROM tasks t WHERE t.id=? AND {active_sql()}',(task['id'],)).fetchone()[0]==0
    assert len(notices(repo))==1 and '尚未验收' in notices(repo)[0]['body_markdown']
    assert notices(repo)[0]['author_type']!='user'
    assert repo.list_task_attachments(d['project']['id'],task['id'])==[]
    assert repo.get_task(d['task']['id'])['status']=='review'
    assert finish(repo,d,run=run)[1]['replayed']
    assert len(summaries(repo))==1 and len(notices(repo))==1
    finish(repo,{**d,'task':task})
    assert len(summaries(repo))==1
    assert '汇总已排队' not in notices(repo)[0]['body_markdown']
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_completion_events').fetchone()[0]==1


def test_blocked_and_review_results_merge_pending_same_room_without_losing_sources(repo,lease_task):
    d=lease_task;finish(repo,d,status='blocked');d2=another(repo,d);finish(repo,d2)
    assert len(summaries(repo))==1 and len(notices(repo))==1
    events=followups.summary_events(repo.connection,summaries(repo)[0]['id'])
    assert [e['source_task_id'] for e in events]==[d['task']['id'],d2['task']['id']]
    assert events[0]['status']=='blocked' and '阻碍' in notices(repo)[0]['body_markdown']
    assert repo.get_task(d['task']['id'])['status']=='blocked'


@pytest.mark.parametrize('summary_state',['prepared','running','completed'])
def test_started_summary_snapshot_is_immutable_and_next_source_queues_separately(repo,lease_task,summary_state):
    d=lease_task;finish(repo,d);first=summaries(repo)[0];store=ExecutionStore(repo)
    run=store.acquire(first['id'],d['device']['id'],'summary-existing-receipt')['execution']
    if summary_state!='prepared':store.start(first['id'],d['device']['id'],run['generation'],run['run_key'])
    if summary_state=='completed':finish(repo,{**d,'task':first},run=run)
    snapshot=followups.summary_events(repo.connection,first['id'])
    d2=another(repo,d);finish(repo,d2)
    assert len(summaries(repo))==2 and len(notices(repo))==2
    assert followups.summary_events(repo.connection,first['id'])==snapshot
    assert followups.summary_events(repo.connection,summaries(repo)[1]['id'])[0]['source_task_id']==d2['task']['id']


@pytest.mark.parametrize('running_summary',[False,True])
def test_ten_late_files_update_one_receipt_and_one_pending_batch(client,repo,settings,lease_task,running_summary):
    d=lease_task;run,_=finish(repo,d);first=summaries(repo)[0]
    snapshot=followups.summary_events(repo.connection,first['id'])
    if running_summary:start(ExecutionStore(repo),{**d,'task':first},'summary-being-prepared-001')
    for index in range(10):
        response=upload_result(client,repo,d,run,name=f'part-{index}.md',body=f'# Part {index}'.encode(),
            key=f'{index:064x}',reference=f'output/part-{index}.md')
        assert response.status_code==200,response.text
    assert len(notices(repo))==1
    assert '10 个成果文件' in notices(repo)[0]['body_markdown']
    assert len(summaries(repo))==(2 if running_summary else 1)
    assert len(followups.summary_events(repo.connection,summaries(repo)[-1]['id'])[0]['artifacts'])==10
    if running_summary:assert followups.summary_events(repo.connection,first['id'])==snapshot
    revision=repo.connection.execute('SELECT revision FROM hub_completion_events').fetchone()[0]
    assert upload_result(client,repo,d,run,name='part-9.md',body=b'# Part 9',key=f'{9:064x}',reference='output/part-9.md').json()['replayed']
    assert repo.connection.execute('SELECT revision FROM hub_completion_events').fetchone()[0]==revision
    assert repo.get_task(d['task']['id'])['board_state']=='archived'


def test_archive_does_not_suppress_closure_and_done_late_file_does_not_restart_it(client,repo,settings,lease_task):
    d=lease_task;store=ExecutionStore(repo,files_root=settings.files_dir);run=start(store,d)
    assert upload_result(client,repo,d,run).status_code==200
    finish(repo,d,store=store,run=run)
    assert repo.get_task(d['task']['id'])['board_state']=='archived'
    assert len(summaries(repo))==1
    finish(repo,{**d,'task':summaries(repo)[0]})
    repo.update_task_status(d['task']['id'],'done','用户验收完成')
    assert upload_result(client,repo,d,run,name='archive.md',body=b'archive',key='b'*64,reference='output/archive.md').status_code==200
    assert len(summaries(repo))==1 and len(notices(repo))==1
    assert '任务已标记完成' in notices(repo)[0]['body_markdown']


def test_notification_savepoint_failure_never_discards_real_delivery(repo,lease_task,monkeypatch):
    original=repo.create_entry
    def fail_notice(*args,**kwargs):
        if 'completion_notice' in kwargs.get('metadata_json',''):raise RuntimeError('isolated notification failure')
        return original(*args,**kwargs)
    monkeypatch.setattr(repo,'create_entry',fail_notice)
    _,result=finish(repo,lease_task)
    assert result['accepted'] and result['execution']['state']=='completed'
    assert repo.get_task(lease_task['task']['id'])['status']=='review'
    assert repo.get_entry(result['execution']['result_entry_id'])['body_markdown']==RESULT['body']
    assert summaries(repo)==[] and notices(repo)==[]
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_completion_events').fetchone()[0]==0
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_completion_retries').fetchone()[0]==1
    monkeypatch.setattr(repo,'create_entry',original)
    assert followups.retry_completion_followups(repo,lease_task['device']['id'],clock=lambda:float('inf'))==1
    assert len(summaries(repo))==1 and len(notices(repo))==1
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_completion_retries').fetchone()[0]==0


@pytest.mark.parametrize('missing',['inactive','unbound'])
def test_unconfigured_controller_still_receives_server_notice_then_reuses_it(repo,lease_task,missing):
    d=lease_task;db=repo.connection
    if missing=='inactive':db.execute('UPDATE hub_agents SET active=0 WHERE id=?',(d['controller']['id'],))
    else:db.execute('UPDATE hub_agents SET device_id=NULL WHERE id=?',(d['controller']['id'],))
    db.commit();finish(repo,d)
    assert summaries(repo)==[] and len(notices(repo))==1
    assert '总控执行连接当前未配置' in notices(repo)[0]['body_markdown']
    notice_id=notices(repo)[0]['id']
    db.execute('UPDATE hub_agents SET active=1,device_id=? WHERE id=?',(d['device']['id'],d['controller']['id']));db.commit()
    with db:
        db.execute('BEGIN IMMEDIATE');followups.queue_completion_followup(repo,d['task']['id'])
    assert len(summaries(repo))==1 and [n['id'] for n in notices(repo)]==[notice_id]


def test_disconnected_controller_still_queues_and_explains_waiting(repo,lease_task):
    d=lease_task
    repo.connection.execute("UPDATE devices SET last_seen_at='2000-01-01T00:00:00+00:00' WHERE id=?",(d['device']['id'],));repo.connection.commit()
    finish(repo,d)
    assert len(summaries(repo))==1 and '汇总已排队' in notices(repo)[0]['body_markdown']


def test_summary_contains_original_bounded_acceptance_not_all_attachments(repo,lease_task):
    d=lease_task;requirements='青绿美丽，金色宜居，蓝金智慧；不允许地标台阶，沿用标题体系。'+'其他原始约束。'*700
    repo.connection.execute('UPDATE tasks SET description=? WHERE id=?',(requirements,d['task']['id']));repo.connection.commit()
    finish(repo,d);task=repo.get_task(summaries(repo)[0]['id'])
    context=d['agents'].context(task);event=context['delivery_events'][0]
    assert event['acceptance_requirements']==requirements[:4000] and event['requirements_excerpted']
    assert context['attachments']==[] and context['conversations']==[]
    prompt=repo.build_task_prompt_markdown(d['project']['slug'],task['id'])
    assert '不允许地标台阶' in prompt and '不得声称已亲自核验画面' in prompt and '不委派' in prompt
    assert len(prompt)<9000


def test_summary_default_model_is_astra_medium_but_user_override_wins(repo,settings):
    project,device,agents,controller,worker,_=setup_team(repo,settings)
    task=repo.create_task(project['id'],'制作结果',assigned_agent_id=worker['id'])
    agents.route(task['id'],{'action':'new','title':'本轮制作','reason':'test'},'codex_cli')
    finish(repo,dict(project=project,device=device,agents=agents,controller=controller,worker=worker,task=task))
    summary=summaries(repo)[0]
    choice=select_model(repo.connection,controller,settings.data_dir,task=summary)
    assert (choice['model'],choice['reasoning_effort'])==('gpt-6-astra','medium')
    save_policy(repo.connection,controller,settings.data_dir,mode='manual',model='gpt-5.6-sol',reasoning_effort='high',actor='user')
    choice=select_model(repo.connection,controller,settings.data_dir,task=summary)
    assert (choice['model'],choice['reasoning_effort'],choice['source'])==('gpt-5.6-sol','high','user')


def test_old_delivery_summary_includes_current_revision_chain_and_latest_correction(client,repo,lease_task):
    d=lease_task;finish(repo,d);summary=summaries(repo)[0];db=repo.connection
    second=another(repo,d);third=another(repo,d)
    owner=db.execute("SELECT id FROM users WHERE username='owner'").fetchone()[0]
    correction=repo.create_task(d['project']['id'],'用户纠正',description='请按原先青绿和金色要求重做',
        assigned_agent_id=d['controller']['id'],created_by_user_id=owner)
    room=repo.get_task(d['task']['id'])['conversation_id']
    d['agents'].route(correction['id'],{'action':'reuse','conversation_id':room,'reason':'修订'},'codex_cli')
    repo.create_entry(d['project']['id'],'你','message','不是让你换一套风格，沿用原来四屏标题体系。',
        author_user_id=owner,task_id=correction['id'],execution_fenced=True)
    for target,successor in [(d['task'],second['task']),(second['task'],third['task'])]:
        db.execute('''INSERT INTO hub_task_amendments
            (source_task_id,target_task_id,next_task_id,request_key,payload_hash,instruction,mode,created_at)
            VALUES (?,?,?,?,?,?,?,?)''',(correction['id'],target['id'],successor['id'],f'revision-{successor["id"]}',
                                       'test','保留最初风格要求','queued','2026-10-29T00:00:00Z'))
    db.commit();repo.update_task_status(third['task']['id'],'in_progress','正在按新要求修改')
    task=repo.get_task(summary['id']);events=followups.summary_events(db,task['id'])
    current=followups._current_room_context(db,task,events)
    assert {row['next_id'] for row in current['explicit_successors']}=={second['task']['id'],third['task']['id']}
    assert any(row['id']==third['task']['id'] and row['status']=='in_progress' for row in current['active_work'])
    prompt=d['agents'].prompt(task)
    assert '沿用原来四屏标题体系' in prompt and '旧轮结果不代表最新要求已经完成' in prompt


def test_schema_repeated_initialization_preserves_records_and_page_reads_do_not_queue(repo,lease_task):
    finish(repo,lease_task);followups.initialize_completion_followups(repo.connection)
    before=repo.connection.total_changes
    task=repo.get_task(summaries(repo)[0]['id']);lease_task['agents'].context(task);lease_task['agents'].prompt(task)
    assert repo.connection.total_changes==before and len(summaries(repo))==1


def test_heartbeat_recovers_missing_controller_and_backoff_is_not_retried_on_every_ping(client,repo,lease_task):
    d=lease_task;db=repo.connection
    db.execute('UPDATE hub_agents SET device_id=NULL WHERE id=?',(d['controller']['id'],));db.commit()
    finish(repo,d)
    row=dict(db.execute('SELECT * FROM hub_completion_retries').fetchone())
    assert row['attempts']==1
    assert client.post('/api/device/heartbeat',headers=token_headers(repo,d['device'])).status_code==200
    assert dict(db.execute('SELECT * FROM hub_completion_retries').fetchone())==row
    db.execute('UPDATE hub_agents SET device_id=? WHERE id=?',(d['device']['id'],d['controller']['id']))
    db.execute('UPDATE hub_completion_retries SET next_attempt=0');db.commit()
    assert client.post('/api/device/heartbeat',headers=token_headers(repo,d['device'])).status_code==200
    assert len(summaries(repo))==1 and len(notices(repo))==1
    assert db.execute('SELECT COUNT(*) FROM hub_completion_retries').fetchone()[0]==0


def test_retry_is_bounded_and_reopened_task_never_reports_old_completion(repo,lease_task):
    d=lease_task;db=repo.connection
    db.execute('UPDATE hub_agents SET device_id=NULL WHERE id=?',(d['controller']['id'],));db.commit()
    sources=[d]+[another(repo,d) for _ in range(5)]
    for source in sources:finish(repo,source)
    assert db.execute('SELECT COUNT(*) FROM hub_completion_retries').fetchone()[0]==6
    assert followups.retry_completion_followups(repo,d['device']['id'],clock=lambda:float('inf'))==4
    assert db.execute('SELECT COUNT(*) FROM hub_completion_retries WHERE attempts=2').fetchone()[0]==4
    for source in sources:repo.update_task_status(source['task']['id'],'open','明确重做')
    db.execute('UPDATE hub_agents SET device_id=? WHERE id=?',(d['device']['id'],d['controller']['id']));db.commit()
    followups.retry_completion_followups(repo,d['device']['id'],clock=lambda:float('inf'))
    followups.retry_completion_followups(repo,d['device']['id'],clock=lambda:float('inf'))
    assert db.execute('SELECT COUNT(*) FROM hub_completion_retries').fetchone()[0]==0
    assert summaries(repo)==[]
