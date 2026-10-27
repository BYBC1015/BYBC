import pytest
from codex_hub.task_board import organize_task
from codex_hub.execution_leases import ExecutionStore,ExecutionConflict
from codex_hub.workspace import _task_views
from test_execution_leases import lease_task,start
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
