"""Server contracts for the lightweight identity-first workspace."""
import re
from urllib.parse import parse_qs, urlsplit

import pytest

import codex_hub.workspace as workspace
from codex_hub.agents import AgentStore
from codex_hub.repository import Repository, utc_now
from workspace_fixture import seed_workspace


def login(client):
    response = client.post('/login', data={'username':'owner','password':'owner-password'}, follow_redirects=False)
    assert response.status_code == 303
    return client.cookies.get('codex_hub_csrf')


@pytest.fixture
def workspace_contexts(monkeypatch):
    """Capture the actual render payload; app middleware omits TestClient debug context."""
    captured = []
    original = workspace.build_workspace_context

    def capture(*args, **kwargs):
        context = original(*args, **kwargs)
        captured.append(context)
        return context

    monkeypatch.setattr(workspace, 'build_workspace_context', capture)
    return captured


def test_workspace_dispatch_uses_selected_identity_and_preserves_room(client, repo):
    data = seed_workspace(repo, room_messages=2)
    csrf = login(client)
    base = '/projects/workspace-team'
    payload = {'_csrf':csrf, 'agent_ids':str(data['reviewer']['id']), 'description':'只交给审查员检查交付',
               'title':'指定身份工作', 'request_key':'shell-single-identity-001'}
    response = client.post(base+'/workspace-dispatch', data=payload, follow_redirects=False)
    assert response.status_code == 303
    room_id = int(response.headers['location'].split('?',1)[0].rsplit('/',1)[-1])
    created = [t for t in repo.list_tasks(data['project']['id']) if t['title']=='指定身份工作']
    assert len(created) == 1
    assert created[0]['assigned_agent_id'] == data['reviewer']['id']
    assert created[0]['assigned_device_id'] == data['connection']['id']
    assert created[0]['conversation_id'] == room_id
    assert client.post(base+'/workspace-dispatch', data=payload, follow_redirects=False).status_code == 303
    assert len([t for t in repo.list_tasks(data['project']['id']) if t['title']=='指定身份工作']) == 1
    followup = {**payload, 'description':'在原对话补充下一项任务', 'title':'原对话后续工作',
                'request_key':'shell-followup-001', 'conversation_id':str(data['conversation_id']),
                'agent_ids':str(data['engineer']['id'])}
    response = client.post(base+'/workspace-dispatch', data=followup, follow_redirects=False)
    assert response.status_code == 303
    task = next(t for t in repo.list_tasks(data['project']['id']) if t['title']=='原对话后续工作')
    assert task['conversation_id'] == data['conversation_id']
    assert task['assigned_agent_id'] == data['engineer']['id']


@pytest.mark.parametrize('invalid', ['empty_recipients','paused','foreign_room','changed_retry'])
def test_invalid_workspace_dispatch_keeps_draft_and_creates_no_tasks(client, repo, invalid, workspace_contexts):
    data = seed_workspace(repo, room_messages=2)
    csrf = login(client)
    payload = {'_csrf':csrf, 'agent_ids':str(data['engineer']['id']), 'description':'保留我的长草稿与换行\n第二段验收内容',
               'title':'需要保留的工作目标', 'request_key':'shell-invalid-001'}
    base = '/projects/workspace-team/workspace-dispatch'
    if invalid == 'empty_recipients':
        payload.pop('agent_ids')
    elif invalid == 'paused':
        payload['agent_ids'] = str(data['paused']['id'])
    elif invalid == 'foreign_room':
        other = repo.create_project(slug='foreign-workspace',name='Other',created_by_device_id=data['connection']['id'])
        foreign_agent = data['store'].create(other['id'],'外部成员','独立项目',device_id=data['connection']['id'])
        task = repo.create_task(other['id'],'独立任务',assigned_agent_id=foreign_agent['id'])
        room = data['store'].route(task['id'],{'action':'new','reason':'其他项目上下文'},'codex_cli')
        payload['conversation_id'] = str(room['conversation_id'])
    else:
        assert client.post(base,data=payload,follow_redirects=False).status_code == 303
        payload['description'] += '\n同一个请求标识不能用于其他目标'
    before = len(repo.list_tasks(data['project']['id']))
    response = client.post(base,data=payload,follow_redirects=False)
    assert response.status_code == 200
    assert workspace_contexts[-1]['dispatch_error']
    assert '保留我的长草稿与换行' in response.text
    assert '第二段验收内容' in response.text
    assert len(repo.list_tasks(data['project']['id'])) == before


def test_workspace_csrf_and_identity_selection_have_no_dispatch_side_effect(client, repo, workspace_contexts):
    data = seed_workspace(repo, room_messages=2)
    login(client)
    before = len(repo.list_tasks(data['project']['id']))
    selected = client.get(f"/projects/workspace-team?agent={data['engineer']['id']}")
    assert selected.status_code == 200
    assert workspace_contexts[-1]['selected_agent']['id'] == data['engineer']['id']
    assert workspace_contexts[-1]['dispatch_draft']['agent_ids'] == [data['controller']['id']]
    assert len(repo.list_tasks(data['project']['id'])) == before
    response = client.post('/projects/workspace-team/workspace-dispatch', data={
        'agent_ids':str(data['engineer']['id']), 'description':'不能无CSRF派单', 'request_key':'no-csrf-dispatch-001'})
    assert response.status_code == 403
    assert len(repo.list_tasks(data['project']['id'])) == before


def test_workspace_load_is_bounded_and_does_not_hydrate_legacy_archive(client, repo, monkeypatch, workspace_contexts):
    data = seed_workspace(repo, room_messages=260, legacy_entries=1481, attachments=1171)
    login(client)

    def forbidden(*args, **kwargs):
        raise AssertionError('default workspace tried to load an unbounded archive')

    for method in ('list_project_entries','list_all_entries','list_entries','list_attachments','list_tasks'):
        monkeypatch.setattr(Repository, method, forbidden)
    response = client.get('/projects/workspace-team')
    assert response.status_code == 200
    assert 'data-ws-shell' in response.text
    assert 'LEGACY-ARCHIVE-' not in response.text
    assert 'ARCHIVE-FILE-' not in response.text
    assert workspace_contexts[-1]['entries'] == []
    response = client.get(f"/projects/workspace-team/conversations/{data['conversation_id']}")
    assert response.status_code == 200
    recent_context = workspace_contexts[-1]
    assert 0 < len(recent_context['entries']) <= 40
    assert recent_context['older_entries_url']
    assert 'LEGACY-ARCHIVE-' not in response.text
    assert 'ARCHIVE-FILE-' not in response.text
    older = client.get(recent_context['older_entries_url'])
    assert older.status_code == 200
    assert 0 < len(workspace_contexts[-1]['entries']) <= 40
    assert {e['id'] for e in recent_context['entries']}.isdisjoint({e['id'] for e in workspace_contexts[-1]['entries']})


def test_workspace_etag_is_stable_until_visible_work_changes(client, repo):
    data = seed_workspace(repo, room_messages=2)
    login(client)
    path = f"/projects/workspace-team/conversations/{data['conversation_id']}"
    first = client.get(path)
    assert first.status_code == 200
    assert first.headers.get('etag')
    same = client.get(path,headers={'If-None-Match':first.headers['etag']})
    assert same.status_code == 304
    repo.create_entry(data['project']['id'], source_label=data['connection']['label'], kind='message',
                      body_markdown='新的任务进展',author_device_id=data['connection']['id'],task_id=data['tasks'][2]['id'])
    changed = client.get(path,headers={'If-None-Match':first.headers['etag']})
    assert changed.status_code == 200
    assert changed.headers['etag'] != first.headers['etag']


def test_only_explicit_assignment_query_changes_the_new_work_recipient(client, repo, workspace_contexts):
    data = seed_workspace(repo, room_messages=2)
    login(client)
    response = client.get(f"/projects/workspace-team?assign_agent={data['reviewer']['id']}")
    assert response.status_code == 200
    assert workspace_contexts[-1]['dispatch_draft']['agent_ids'] == [data['reviewer']['id']]
    response = client.get(f"/projects/workspace-team?assign_agent={data['paused']['id']}")
    assert response.status_code == 200
    assert workspace_contexts[-1]['dispatch_draft']['agent_ids'] == []


def test_archive_keeps_blocked_and_cancelled_out_of_running_and_completed_columns(client, repo):
    data = seed_workspace(repo, room_messages=2)
    login(client)
    response = client.get('/projects/workspace-team?view=archive')
    assert response.status_code == 200
    assert 'data-agent-workspace' in response.text
    columns = dict(re.findall(r'<div class="kanban-column" data-task-statuses="([^"]+)">(.*?)(?=<div class="kanban-column"|</section>)', response.text, re.S))
    assert '接口连接遇阻' in columns['blocked']
    assert '接口连接遇阻' not in columns['in_progress']
    assert '已取消的试作' in columns['cancelled']
    assert '已取消的试作' not in columns['done']
    assert '交付说明待复核' in columns['review']


def test_legacy_task_forms_return_to_relevant_identity_task_or_device_archive(client, repo):
    data = seed_workspace(repo, room_messages=2)
    csrf = login(client)
    agent_result = client.post('/projects/workspace-team/tasks', data={
        '_csrf':csrf, 'title':'智能体表单派单', 'assigned_agent_id':str(data['engineer']['id']),
    }, follow_redirects=False)
    assert agent_result.status_code == 303
    created = next(task for task in repo.list_tasks(data['project']['id']) if task['title']=='智能体表单派单')
    assert agent_result.headers['location'] == f"/projects/workspace-team/tasks/{created['id']}"
    legacy_result = client.post('/projects/workspace-team/tasks', data={
        '_csrf':csrf, 'title':'旧设备表单派单', 'assigned_device_id':str(data['connection']['id']),
    }, follow_redirects=False)
    assert legacy_result.status_code == 303
    assert legacy_result.headers['location'] == '/projects/workspace-team?view=archive#tasks'


def test_workspace_room_pages_keep_current_room_entry_cursor_and_project_scope(client, repo, workspace_contexts):
    data = seed_workspace(repo, room_messages=100)
    foreign = repo.create_project(slug='unrelated-room-team', name='Other', created_by_device_id=data['connection']['id'])
    stamp = utc_now()
    repo.connection.executemany('INSERT INTO hub_conversations (project_id,title,created_at) VALUES (?,?,?)',
        [(data['project']['id'], f'分页工作对话 {index:03d}', stamp) for index in range(120)] +
        [(foreign['id'], f'外部项目对话 {index:03d}', stamp) for index in range(55)])
    repo.connection.commit()
    login(client)
    path = f"/projects/workspace-team/conversations/{data['conversation_id']}?before={data['last_entry_id']}&agent={data['engineer']['id']}"
    assert client.get(path).status_code == 200
    first = workspace_contexts[-1]
    assert first['room_page_count'] == 50 and first['room_total_count'] == 121
    assert len(first['agent_conversations']) == 51
    assert [c['id'] for c in first['agent_conversations'] if c.get('outside_room_page')] == [data['conversation_id']]
    page_ids = lambda context: {c['id'] for c in context['agent_conversations'] if not c.get('outside_room_page')}
    seen = page_ids(first)
    older_url = first['older_rooms_url']
    for count in [50, 21]:
        assert client.get(older_url).status_code == 200
        current = workspace_contexts[-1]
        assert current['room_page_count'] == count
        assert seen.isdisjoint(page_ids(current))
        seen.update(page_ids(current))
        assert all(c['project_id'] == data['project']['id'] for c in current['agent_conversations'])
        query = parse_qs(urlsplit(current['workspace_url']).query)
        assert query['before'] == [str(data['last_entry_id'])]
        assert query['agent'] == [str(data['engineer']['id'])]
        assert query['rooms_before']
        assert parse_qs(urlsplit(current['older_entries_url']).query)['rooms_before'] == query['rooms_before']
        assert current['current_conversation']['id'] == data['conversation_id']
        older_url = current['older_rooms_url']
    assert len(seen) == 121 and not older_url
    assert client.get(current['latest_rooms_url']).status_code == 200
    assert page_ids(workspace_contexts[-1]) == page_ids(first)
    assert 'rooms_before' not in parse_qs(urlsplit(workspace_contexts[-1]['workspace_url']).query)
