"""Workspace files are task evidence, never an implicit project-wide file pool."""
import hashlib
import json
import shutil

import pytest
from fastapi import HTTPException

import codex_hub.main as main
import codex_hub.workspace as workspace
from codex_hub.agents import AgentStore
from codex_hub.repository import Repository
from codex_hub.security import hash_password
from test_agents import token_headers
from test_workspace_shell import login
from workspace_fixture import seed_workspace


@pytest.fixture
def upload_team(client, repo):
    data = seed_workspace(repo, room_messages=0)
    data['csrf'] = login(client)
    return data


def payload(data, **changes):
    return {'_csrf':data['csrf'], 'agent_ids':[str(data['engineer']['id']),str(data['reviewer']['id'])],
            'description':'请依据附件核对交付', 'request_key':'attachment-work-0001', **changes}


def post_files(client, data, *, files=None, **changes):
    return client.post('/projects/workspace-team/workspace-dispatch', data=payload(data, **changes),
        files=files if files is not None else [('attachments', ('brief.txt', b'project brief', 'text/plain'))],
        follow_redirects=False)


def counts(repo, settings):
    tables = ('tasks','entries','attachments','hub_task_attachments','hub_conversations','hub_agent_dispatches','hub_dispatch_tasks')
    return {**{table:repo.connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in tables},
            'files':sorted(str(path.relative_to(settings.files_dir)) for path in settings.files_dir.rglob('*') if path.is_file())}


def created_tasks(repo, data):
    initial = {task['id'] for task in data['tasks']}
    return [task for task in repo.list_tasks(data['project']['id']) if task['id'] not in initial]


def test_group_files_share_one_physical_copy_and_user_entry_with_exact_task_links(client, repo, settings, upload_team):
    data = upload_team
    result = post_files(client, data, conversation_id=str(data['conversation_id']), files=[
        ('attachments', ('brief.txt', b'project brief', 'text/plain')),
        ('attachments', ('../../image.png', b'png-fixture', 'image/png'))])
    assert result.status_code == 303
    tasks = created_tasks(repo, data)
    assert len(tasks) == 2
    assert {task['conversation_id'] for task in tasks} == {data['conversation_id']}
    manifests = [repo.list_task_attachments(data['project']['id'], task['id']) for task in tasks]
    assert [file['id'] for file in manifests[0]] == [file['id'] for file in manifests[1]]
    assert {file['original_filename'] for file in manifests[0]} == {'brief.txt','image.png'}
    assert len({file['entry_id'] for file in manifests[0]}) == 1
    entry = repo.get_entry(manifests[0][0]['entry_id'])
    assert entry['author_type'] == 'user' and entry['body_markdown'] == '请依据附件核对交付'
    assert set(json.loads(entry['metadata_json'])['task_ids']) == {task['id'] for task in tasks}
    assert len(counts(repo, settings)['files']) == 2
    page = client.get(result.headers['location'])
    assert page.status_code == 200 and 'brief.txt' in page.text and 'image.png' in page.text
    assert data['store'].context(tasks[1])['attachments'] == manifests[1]
    assert 'brief.txt' in data['store'].prompt(tasks[1])
    assert repo.list_task_attachments(data['project']['id'], data['tasks'][2]['id']) == []


def test_file_only_dispatch_has_a_real_task_and_downloadable_file(client, repo, upload_team):
    data = upload_team
    result = post_files(client, data, description='', agent_ids=str(data['engineer']['id']))
    assert result.status_code == 303
    task = created_tasks(repo, data)[0]
    assert task['title'] == '处理附件：brief.txt'
    manifest = repo.list_task_attachments(data['project']['id'], task['id'])
    downloaded = client.get(f"/api/projects/workspace-team/attachments/{manifest[0]['id']}",
                            headers=token_headers(repo, data['connection']))
    assert downloaded.status_code == 200 and downloaded.content == b'project brief'
    assert manifest[0]['sha256'] == hashlib.sha256(downloaded.content).hexdigest()


def test_exact_file_retry_and_reordered_files_do_not_even_write_temporary_copies(client, repo, settings, upload_team, monkeypatch):
    data = upload_team
    files = [('attachments', ('one.txt', b'one', 'text/plain')), ('attachments', ('two.txt', b'two', 'text/plain'))]
    first = post_files(client, data, files=files)
    assert first.status_code == 303
    before = counts(repo, settings)
    async def forbidden(*args, **kwargs):
        raise AssertionError('an exact file retry must not write another physical file')
    monkeypatch.setattr(main, 'store_attachment_file', forbidden)
    retried = post_files(client, data, files=list(reversed(files)))
    assert retried.status_code == 303 and retried.headers['location'] == first.headers['location']
    assert counts(repo, settings) == before


@pytest.mark.parametrize('filename,content,content_type', [
    ('changed.txt', b'project brief', 'text/plain'), ('brief.txt', b'changed', 'text/plain'),
    ('brief.txt', b'project brief', 'application/octet-stream')])
def test_retry_digest_includes_name_content_and_type(client, repo, settings, upload_team, filename, content, content_type):
    data = upload_team
    assert post_files(client, data).status_code == 303
    before = counts(repo, settings)
    response = post_files(client, data, files=[('attachments',(filename,content,content_type))])
    assert response.status_code == 200 and '同一请求标识不能用于不同任务或附件' in response.text
    assert counts(repo, settings) == before


@pytest.mark.parametrize('limit_case', ['count','configured_single','hard_single','total'])
def test_file_limits_reject_atomically_before_storage(client, repo, settings, upload_team, monkeypatch, limit_case):
    data = upload_team
    files = [('attachments', ('brief.txt', b'12345', 'text/plain'))]
    status = 413
    if limit_case == 'count':
        files = [('attachments',(f'{number}.txt',b'x','text/plain')) for number in range(13)]
        status = 200
    elif limit_case == 'configured_single':
        settings.max_upload_bytes = 4
    elif limit_case == 'hard_single':
        monkeypatch.setattr(workspace,'WORKSPACE_MAX_FILE_BYTES',4)
        settings.max_upload_bytes = 100
    else:
        monkeypatch.setattr(workspace,'WORKSPACE_MAX_TOTAL_BYTES',8)
        files += [('attachments', ('second.txt', b'67890', 'text/plain'))]
    before = counts(repo, settings)
    response = post_files(client, data, files=files)
    assert response.status_code == status
    assert 'data-ws-dispatch-error' in response.text and '请依据附件核对交付' in response.text
    assert counts(repo, settings) == before


@pytest.mark.parametrize('failure', ['csrf','foreign_agent','foreign_room','paused'])
def test_upload_destinations_and_csrf_are_checked_without_side_effects(client, repo, settings, upload_team, failure):
    data = upload_team
    changes = {}
    if failure == 'csrf':
        changes['_csrf'] = 'incorrect'
    elif failure == 'paused':
        changes['agent_ids'] = str(data['paused']['id'])
    else:
        other = repo.create_project(slug='private-files',name='Other',created_by_device_id=data['connection']['id'])
        agent = data['store'].create(other['id'],'其他项目成员','私有项目',device_id=data['connection']['id'])
        if failure == 'foreign_agent':
            changes['agent_ids'] = str(agent['id'])
        else:
            origin = repo.create_task(other['id'],'其他工作',assigned_agent_id=agent['id'])
            room = data['store'].route(origin['id'],{'action':'new','reason':'独立工作'},'codex_cli')
            changes['conversation_id'] = str(room['conversation_id'])
    before = counts(repo, settings)
    response = post_files(client,data,**changes)
    assert response.status_code == (403 if failure == 'csrf' else 200)
    assert counts(repo, settings) == before


def test_partial_storage_failure_removes_first_file_and_keeps_all_database_state(client, repo, settings, upload_team, monkeypatch):
    data = upload_team
    original = main.store_attachment_file
    calls = 0
    async def broken(request,project,file):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise HTTPException(413,'文件存储失败测试')
        return await original(request,project,file)
    monkeypatch.setattr(main,'store_attachment_file',broken)
    before = counts(repo, settings)
    response = post_files(client,data,files=[('attachments',('a.txt',b'a','text/plain')),('attachments',('b.txt',b'b','text/plain'))])
    assert response.status_code == 413 and calls == 2
    assert counts(repo, settings) == before


def test_failed_attachment_binding_rolls_back_tasks_entry_metadata_and_physical_files(client, repo, settings, upload_team, monkeypatch):
    data = upload_team
    original = Repository.bind_task_attachments
    def broken(self,*args,**kwargs):
        original(self,*args,**kwargs)
        raise ValueError('原子绑定失败测试')
    monkeypatch.setattr(Repository,'bind_task_attachments',broken)
    before = counts(repo, settings)
    response = post_files(client,data)
    assert response.status_code == 200 and '原子绑定失败测试' in response.text
    assert counts(repo, settings) == before


def test_dispatch_result_read_failure_before_commit_does_not_leave_dangling_files(client, repo, settings, upload_team, monkeypatch):
    data = upload_team
    original = Repository.get_task
    def broken(self,task_id):
        if self.connection.execute('SELECT 1 FROM hub_task_attachments WHERE task_id=?',(task_id,)).fetchone():
            raise ValueError('读取结果失败测试')
        return original(self,task_id)
    monkeypatch.setattr(Repository,'get_task',broken)
    before = counts(repo, settings)
    response = post_files(client,data)
    assert response.status_code == 200 and '读取结果失败测试' in response.text
    assert counts(repo, settings) == before


def test_racing_duplicate_dispatch_removes_only_its_own_uncommitted_copy(client, repo, settings, upload_team, monkeypatch):
    data = upload_team
    original = AgentStore.dispatch
    def race(self, repository, project_id, **kwargs):
        staged = kwargs['attachments'][0]
        winner = {**staged, 'stored_relpath':staged['stored_relpath']+'.winner'}
        shutil.copyfile(settings.files_dir/staged['stored_relpath'], settings.files_dir/winner['stored_relpath'])
        original(self,repository,project_id,**{**kwargs,'attachments':[winner]})
        return original(self,repository,project_id,**kwargs)
    monkeypatch.setattr(AgentStore,'dispatch',race)
    assert post_files(client,data).status_code == 303
    files = counts(repo, settings)['files']
    assert len(files) == 1 and files[0].endswith('.winner')
    assert len(created_tasks(repo,data)) == 2
    assert (settings.files_dir/files[0]).read_bytes() == b'project brief'


def test_manifest_requires_bound_device_or_project_management_and_does_not_leak_paths(client, repo, upload_team):
    data = upload_team
    assert post_files(client,data).status_code == 303
    task = created_tasks(repo,data)[0]
    path = f"/api/projects/workspace-team/tasks/{task['id']}/attachments"
    assert client.get(path).status_code == 401
    own = client.get(path,headers=token_headers(repo,data['connection']))
    assert own.status_code == 200 and len(own.json()['attachments']) == 1
    assert 'stored_relpath' not in own.text and 'files_dir' not in own.text
    unrelated = repo.create_device(label='Unrelated writer')
    repo.add_project_member(data['project']['id'],unrelated['id'],'write')
    assert client.get(path,headers=token_headers(repo,unrelated)).status_code == 403
    repo.add_project_member(data['project']['id'],unrelated['id'],'admin')
    assert client.get(path,headers=token_headers(repo,unrelated)).status_code == 200
    other = repo.create_project(slug='manifest-foreign',name='Other',created_by_device_id=unrelated['id'])
    assert client.get(f"/api/projects/{other['slug']}/tasks/{task['id']}/attachments",headers=token_headers(repo,unrelated)).status_code == 404


def test_controller_delegation_inherits_only_source_task_files_with_explicit_subset_and_empty_override(client, repo, settings, upload_team):
    data = upload_team
    files = [('attachments',('one.txt',b'one','text/plain')),('attachments',('two.txt',b'two','text/plain'))]
    assert post_files(client,data,files=files,agent_ids=str(data['controller']['id'])).status_code == 303
    source = created_tasks(repo,data)[0]
    ids = [file['id'] for file in repo.list_task_attachments(data['project']['id'],source['id'])]
    url = f"/api/projects/workspace-team/tasks/{source['id']}/delegate"
    headers = token_headers(repo,data['connection'])
    for index, selected in enumerate([None, [ids[0]], []]):
        body = {'assigned_agent_id':data['engineer']['id'],'title':'交给工程师检查','request_key':f'files-delegate-{index}'}
        if selected is not None:
            body['attachment_ids'] = selected
        response = client.post(url,headers=headers,json=body)
        assert response.status_code == 201
        task = response.json()['task']
        manifest = repo.list_task_attachments(data['project']['id'],task['id'])
        assert {file['id'] for file in manifest} == set(ids if selected is None else selected)
        assert all(file['source_task_id'] == source['id'] for file in manifest)
        before = counts(repo,settings)
        retry = client.post(url,headers=headers,json=body)
        assert retry.status_code == 201 and retry.json()['task']['id'] == task['id']
        assert counts(repo,settings) == before
        changed = {**body,'attachment_ids':[] if selected != [] else [ids[0]]}
        assert client.post(url,headers=headers,json=changed).status_code == 422
    assert len(counts(repo,settings)['files']) == 2
    assert post_files(client,data,request_key='unrelated-files-001',files=[('attachments',('other.txt',b'other','text/plain'))]).status_code == 303
    unrelated_task = created_tasks(repo,data)[0]
    other_file = repo.list_task_attachments(data['project']['id'],unrelated_task['id'])[0]['id']
    before = counts(repo,settings)
    assert client.post(url,headers=headers,json={'assigned_agent_id':data['engineer']['id'],
        'title':'不能借用其他任务附件','request_key':'files-forbidden-001','attachment_ids':[other_file]}).status_code == 422
    assert counts(repo,settings) == before


def test_source_task_attachment_inheritance_cannot_cross_project_or_room(repo, settings, client, upload_team):
    data = upload_team
    assert post_files(client,data,agent_ids=str(data['controller']['id'])).status_code == 303
    source = created_tasks(repo,data)[0]
    other = repo.create_project(slug='foreign-inheritance',name='Other',created_by_device_id=data['connection']['id'])
    agent = data['store'].create(other['id'],'其他总控','独立项目',kind='controller',device_id=data['connection']['id'])
    before = counts(repo,settings)
    for project_id, agent_id, room_id in [(other['id'],agent['id'],source['conversation_id']),
                                         (data['project']['id'],data['engineer']['id'],data['conversation_id'])]:
        with pytest.raises(ValueError,match='源任务'):
            data['store'].dispatch(repo,project_id,agent_ids=[agent_id],title='不能越界继承',description='',
                request_key=f'inherit-invalid-{project_id}',conversation_id=room_id,source_task_id=source['id'])
    assert counts(repo,settings) == before


def test_read_only_dispatch_receipt_recovers_committed_upload_without_creating_work(client, repo, settings, upload_team):
    data = upload_team
    path = '/projects/workspace-team/workspace-dispatch/attachment-work-0001'
    before = counts(repo, settings)
    assert client.get(path).json() == {'found':False}
    assert counts(repo, settings) == before
    sent = post_files(client, data)
    assert sent.status_code == 303
    before = counts(repo, settings)
    receipt = client.get(path)
    assert receipt.status_code == 200
    assert receipt.json() == {'found':True, 'conversation_url':sent.headers['location'],
                              'task_ids':sorted(task['id'] for task in created_tasks(repo,data))}
    assert counts(repo, settings) == before
    assert client.get('/projects/workspace-team/workspace-dispatch/short').json() == {'found':False}
    assert counts(repo, settings) == before


def test_dispatch_receipt_is_scoped_to_the_project_and_initiating_web_user(client, repo, settings, upload_team):
    data = upload_team
    assert post_files(client,data).status_code == 303
    other_project = repo.create_project(slug='other-receipts',name='Other',created_by_device_id=data['connection']['id'])
    other_user = repo.connection.execute("INSERT INTO users(username,password_hash,role,created_at,updated_at) VALUES (?,?, 'owner',?,?)",
        ('other-user',hash_password('other-password'),'2026-10-27','2026-10-27')).lastrowid
    repo.connection.commit()
    assert other_project and other_user
    before = counts(repo, settings)
    assert client.get('/projects/other-receipts/workspace-dispatch/attachment-work-0001').json() == {'found':False}
    client.cookies.clear()
    assert client.post('/login',data={'username':'other-user','password':'other-password'},follow_redirects=False).status_code == 303
    path = '/projects/workspace-team/workspace-dispatch/attachment-work-0001'
    assert client.get(path).json() == {'found':False}
    assert counts(repo, settings) == before
    client.cookies.clear()
    assert client.get(path,follow_redirects=False).status_code in {303,401}
    assert counts(repo, settings) == before
