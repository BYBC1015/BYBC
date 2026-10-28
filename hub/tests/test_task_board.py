import pytest
from codex_hub.task_board import organize_task,archive_completed_delivery
from codex_hub.execution_leases import ExecutionStore,ExecutionConflict
from codex_hub.workspace import _task_views
from test_execution_leases import lease_task,start,RESULT
from test_work_delivery import upload_result,controller_source
from test_agents import token_headers
from test_workspace_shell import login
from workspace_fixture import seed_workspace


def test_archive_restore_preserve_work_status_messages_and_experiences(repo):
    d=seed_workspace(repo,room_messages=2);task=d['tasks'][1]
    before={table:repo.connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
            for table in ('entries','attachments','hub_agent_experiences')}
    for _ in range(2):organize_task(repo,d['project']['id'],task['id'],'archive')
    assert repo.get_task(task['id'])['status']=='blocked'
    assert task['id'] not in {t['id'] for t in _task_views(repo,d['project']['id'],board='active')}
    organize_task(repo,d['project']['id'],task['id'],'restore')
    assert task['id'] in {t['id'] for t in _task_views(repo,d['project']['id'],board='active')}
    assert before=={table:repo.connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in before}


def test_done_and_controller_replies_move_to_history_but_chat_is_still_visible(client,repo):
    d=seed_workspace(repo,room_messages=2);task=d['tasks'][0]
    repo.update_task_status(task['id'],'review','已答复')
    repo.create_entry(d['project']['id'],'总控','message','原对话里的真实答复',task_id=task['id'],author_device_id=d['connection']['id'])
    login(client);url=f"/projects/workspace-team/conversations/{d['conversation_id']}"
    page=client.get(url)
    assert '原对话里的真实答复' in page.text
    assert f'data-ws-task-card="{task["id"]}"' not in page.text
    history=client.get(url+'?task_history=1')
    assert f'data-ws-task-card="{task["id"]}"' in history.text and '已回复' in history.text
    assert d['controller']['id'] in {a['id'] for a in d['store'].list(d['project']['id']) if a['task_count']==0}


def test_older_active_work_not_displaced_by_many_completed_tasks(repo):
    d=seed_workspace(repo,room_messages=0)
    for i in range(85):repo.create_task(d['project']['id'],f'Historical {i}',status='done')
    active=_task_views(repo,d['project']['id'],board='active')
    assert d['tasks'][1]['id'] in {t['id'] for t in active}


def test_history_pages_keep_all_records_and_message_task_detail(client,repo):
    import re
    d=seed_workspace(repo,room_messages=0)
    historical=[]
    for i in range(35):
        task=repo.create_task(d['project']['id'],f'History {i}',status='done')
        repo.connection.execute('UPDATE tasks SET conversation_id=? WHERE id=?',(d['conversation_id'],task['id']))
        historical.append(task['id'])
    repo.connection.commit()
    repo.create_entry(d['project']['id'],'工程师','message','归档交付仍然可以查看',task_id=historical[0])
    login(client)
    base=f"/projects/workspace-team/conversations/{d['conversation_id']}"
    current=client.get(base).text
    assert f'data-ws-task-panel="{historical[0]}"' in current
    first=client.get(base+'?task_history=1').text
    first_ids=[int(value) for value in re.findall(r'data-ws-task-card="(\d+)"',first)]
    assert len(first_ids)==30
    second=client.get(base+f'?task_history=1&tasks_before={min(first_ids)}').text
    second_ids=[int(value) for value in re.findall(r'data-ws-task-card="(\d+)"',second)]
    assert not set(first_ids)&set(second_ids)
    assert set(historical).issubset(set(first_ids+second_ids))


def test_only_reviewed_delivery_can_be_completed_and_reopen_resurfaces(repo):
    d=seed_workspace(repo,room_messages=0)
    with pytest.raises(ValueError):organize_task(repo,d['project']['id'],d['tasks'][1]['id'],'complete')
    task=d['tasks'][2];organize_task(repo,d['project']['id'],task['id'],'complete')
    assert repo.get_task(task['id'])['status']=='done'
    organize_task(repo,d['project']['id'],task['id'],'complete')
    repo.update_task_status(task['id'],'open','重新开始')
    assert repo.get_task(task['id'])['board_state']=='auto'
    assert task['id'] in {t['id'] for t in _task_views(repo,d['project']['id'],board='active')}


def test_running_and_uncertain_work_cannot_be_hidden(repo,lease_task):
    d=lease_task;store=ExecutionStore(repo);run=start(store,d)
    with pytest.raises(ExecutionConflict):organize_task(repo,d['project']['id'],d['task']['id'],'archive')
    repo.connection.execute("UPDATE hub_task_executions SET state='uncertain' WHERE task_id=?",(d['task']['id'],));repo.connection.commit()
    repo.update_task_status(d['task']['id'],'blocked','连接结果待核对')
    with pytest.raises(ExecutionConflict):organize_task(repo,d['project']['id'],d['task']['id'],'archive')
    assert repo.get_task(d['task']['id'])['board_state']=='auto'
    repo.update_task_status(d['task']['id'],'cancelled','取消仍需等待进程停止')
    assert d['task']['id'] in {t['id'] for t in _task_views(repo,d['project']['id'],board='active')}
    store.acknowledge_stopped(d['task']['id'],d['device']['id'],run['generation'],run['run_key'])
    assert d['task']['id'] not in {t['id'] for t in _task_views(repo,d['project']['id'],board='active')}


def test_web_action_requires_owner_session_csrf_and_correct_project(client,repo):
    d=seed_workspace(repo,room_messages=0);task=d['tasks'][2]
    url=f'/projects/workspace-team/tasks/{task["id"]}/organize'
    assert client.post(url,data={'action':'archive'},follow_redirects=False).status_code in {303,401,403}
    csrf=login(client)
    assert client.post(url,data={'action':'archive'}).status_code==403
    response=client.post(url,data={'action':'archive','_csrf':csrf},headers={'Accept':'application/json'})
    assert response.status_code==200 and response.json()['board_state']=='archived'
    other=repo.create_project('other-project','Other')
    assert client.post(f'/projects/other-project/tasks/{task["id"]}/organize',data={'action':'restore','_csrf':csrf}).status_code==404


def test_real_completion_auto_archives_hosted_delivery_and_keeps_chat_preview(client,repo,lease_task):
    d=lease_task;store=ExecutionStore(repo);run=start(store,d)
    uploaded=upload_result(client,repo,d,run)
    assert uploaded.status_code==200
    response=client.post(f"/api/projects/{d['project']['slug']}/tasks/{d['task']['id']}/execution/complete",
        headers=token_headers(repo,d['device']),json={**RESULT,**run,'body':'[实际交付](output/report.md)'})
    assert response.status_code==200,response.text
    task=repo.get_task(d['task']['id'])
    assert task['status']=='review' and task['board_state']=='archived' and task['board_archive_reason']=='delivered'
    assert task['id'] not in {t['id'] for t in _task_views(repo,d['project']['id'],board='active')}
    history=_task_views(repo,d['project']['id'],board='history')
    assert next(t for t in history if t['id']==task['id'])['board_label']=='已交付 · 未验收'
    login(client)
    room=client.get(f"/projects/{d['project']['slug']}/conversations/{task['conversation_id']}")
    assert '实际交付' in room.text
    assert client.get(uploaded.json()['url']+'/preview').status_code==200


def test_late_artifact_archives_once_but_explicit_restore_survives_retry(client,repo,settings,lease_task):
    d=lease_task;store=ExecutionStore(repo,files_root=settings.files_dir);run=start(store,d)
    store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],**RESULT)
    assert repo.get_task(d['task']['id'])['board_state']=='auto'
    assert upload_result(client,repo,d,run).status_code==200
    assert repo.get_task(d['task']['id'])['board_state']=='archived'
    organize_task(repo,d['project']['id'],d['task']['id'],'restore')
    assert upload_result(client,repo,d,run).status_code==200
    replay=store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],**RESULT)
    assert replay['replayed'] and repo.get_task(d['task']['id'])['board_state']=='active'
    assert repo.get_task(d['task']['id'])['board_archive_reason']==''
    # A real new execution cycle, rather than another render/retry, reenables auto organization.
    repo.update_task_status(d['task']['id'],'open','新增修改')
    new=store.acquire(d['task']['id'],d['device']['id'],'next-delivery-generation-001')['execution']
    store.start(d['task']['id'],d['device']['id'],new['generation'],new['run_key'])
    assert upload_result(client,repo,d,new).status_code==200
    store.complete(d['task']['id'],d['device']['id'],new['generation'],new['run_key'],**RESULT)
    assert repo.get_task(d['task']['id'])['board_state']=='archived'


@pytest.mark.parametrize('failure',['missing_file','missing_root','waiting','in_progress','uncertain','old_generation'])
def test_auto_archive_requires_current_complete_visible_delivery(client,repo,settings,lease_task,failure):
    d=lease_task;store=ExecutionStore(repo);run=start(store,d)
    response=upload_result(client,repo,d,run);assert response.status_code==200
    if failure not in {'in_progress','uncertain'}:
        store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],**RESULT)
    if failure=='missing_file':
        path=repo.connection.execute('SELECT stored_relpath FROM attachments WHERE id=?',(response.json()['attachment_id'],)).fetchone()[0]
        (settings.files_dir/path).unlink()
    if failure=='waiting':
        repo.connection.execute("UPDATE tasks SET coordination_state='waiting' WHERE id=?",(d['task']['id'],));repo.connection.commit()
    if failure=='uncertain':
        repo.connection.execute("UPDATE hub_task_executions SET state='uncertain' WHERE task_id=?",(d['task']['id'],));repo.connection.commit()
    if failure=='old_generation':
        repo.update_task_status(d['task']['id'],'open','new work')
        new=store.acquire(d['task']['id'],d['device']['id'],'new-generation-no-files')['execution']
        store.start(d['task']['id'],d['device']['id'],new['generation'],new['run_key'])
        store.complete(d['task']['id'],d['device']['id'],new['generation'],new['run_key'],**RESULT)
    with repo.connection:
        repo.connection.execute('BEGIN IMMEDIATE')
        assert archive_completed_delivery(repo,d['task']['id'],None if failure=='missing_root' else settings.files_dir)==[]
    assert repo.get_task(d['task']['id'])['board_state']=='auto'


@pytest.mark.parametrize('restored',[False,True])
def test_only_explicit_successor_retires_old_block_without_accepting_it(client,repo,settings,lease_task,restored):
    from codex_hub.work_delivery import revise_task
    d=lease_task;store=ExecutionStore(repo,files_root=settings.files_dir);run=start(store,d)
    source,control_run=controller_source(repo,d)
    amendment=revise_task(repo,source,d['device']['id'],{'target_task_id':d['task']['id'],
        'instruction':'接续修复原目标','request_key':'board-followup-work-001','execution':control_run})
    successor=repo.get_task(amendment['next_task_id'])
    store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],
        **{**RESULT,'status':'blocked','summary':'保留原问题'})
    if restored:organize_task(repo,d['project']['id'],d['task']['id'],'restore')
    unrelated=repo.create_task(d['project']['id'],d['task']['title'],status='blocked',assigned_agent_id=d['worker']['id'])
    next_data={**d,'task':successor};new=start(store,next_data,'successor-delivery-001')
    assert upload_result(client,repo,next_data,new).status_code==200
    store.complete(successor['id'],d['device']['id'],new['generation'],new['run_key'],**RESULT)
    old=repo.get_task(d['task']['id'])
    assert old['status']=='blocked' and old['result_summary']=='保留原问题'
    assert old['board_state']==('active' if restored else 'archived')
    assert old['board_archive_reason']==('' if restored else 'superseded:'+str(successor['id']))
    assert repo.get_task(unrelated['id'])['board_state']=='auto'


def test_task_list_reads_have_no_implicit_database_writes(repo):
    d=seed_workspace(repo,room_messages=2)
    before=repo.connection.total_changes
    for board in ('active','history','all'):
        _task_views(repo,d['project']['id'],board=board)
    assert repo.connection.total_changes==before


def test_board_reason_migration_retains_explicit_restore_and_existing_records(repo):
    from codex_hub.task_board import initialize_task_board_schema
    d=seed_workspace(repo,room_messages=2);task=d['tasks'][1]
    organize_task(repo,d['project']['id'],task['id'],'restore')
    count=repo.connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
    repo.connection.execute('ALTER TABLE tasks DROP COLUMN board_archive_reason');repo.connection.commit()
    initialize_task_board_schema(repo.connection);initialize_task_board_schema(repo.connection)
    assert repo.connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==count
    task=repo.get_task(task['id'])
    assert task['board_state']=='active' and task['board_archive_reason']=='' and task['status']=='blocked'
