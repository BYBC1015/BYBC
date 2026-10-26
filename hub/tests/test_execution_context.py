from datetime import datetime, timezone, timedelta
import json

from codex_hub.execution_context import annotate_execution, select_experiences, execution_identity, room_history
from codex_hub.model_catalog import normalize_device_capabilities
from codex_hub.main import annotate_membership_presence


def test_browser_online_is_not_executor_online_and_task_binding_is_frozen():
    now = datetime.now(timezone.utc).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    members = annotate_membership_presence([
        dict(device_id=1, device_label='BYBC-YOLO', permission='write', agent_tasks_only=1,
             last_web_seen_at=now, last_agent_seen_at=old, last_seen_at=now),
        dict(device_id=2, device_label='BYBC-PRO', permission='write', agent_tasks_only=1,
             last_agent_seen_at=now, last_seen_at=now),
    ])
    agents = [dict(id=3, device_id=2, active=1, can_dispatch=True)]
    tasks = [dict(id=4, status='in_progress', assigned_agent_id=3, assigned_device_id=1),
             dict(id=5, status='open', assigned_agent_id=3, assigned_device_id=1)]
    annotate_execution(agents, tasks, members)
    assert agents[0]['connection_state'] == 'connected'
    assert tasks[0]['execution_label'] == '执行连接中断'
    assert tasks[1]['execution_label'] == '排队中 · 等待连接'
    assert tasks[0]['assigned_device_id'] == 1


def test_presence_is_not_proof_of_durable_worker_and_paused_identity():
    agents = [dict(id=1, device_id=7, active=1, can_dispatch=True),
              dict(id=2, device_id=7, active=0, can_dispatch=False)]
    annotate_execution(agents, [], [dict(device_id=7, is_agent_online=True)])
    assert agents[0]['connection_state'] == 'unverified'
    assert agents[1]['connection_state'] == 'paused'


def test_relevance_source_title_budget_and_provenance():
    records = [dict(id=i, task_id=i, revision=2, outcome='review', verification='needs_review',
                    source_title='附件上传', summary='文件哈希核对'*300, worked='完整保存', failed='', next_time='重试去重')
               for i in range(20)]
    result = select_experiences(records, '优化附件上传')
    assert 1 <= len(result) <= 4
    assert len(json.dumps(result, ensure_ascii=False, separators=(',', ':'))) < 6100
    assert result[0]['id'] == 19 and result[0]['revision'] == 2
    assert result[0]['verification'] == 'needs_review' and result[0]['excerpted']
    assert select_experiences(records, '剪辑配乐风格') == []
    assert len(records[0]['summary']) > 600


def test_same_relevance_prefers_verified_record_then_recency():
    records = [dict(id=1, summary='upload', verification='accepted'),
               dict(id=2, summary='upload', verification='needs_review')]
    assert select_experiences(records, 'upload')[0]['id'] == 1


def test_execution_context_preserves_identity_instructions_and_file_manifest_without_duplicate_task():
    ctx = dict(agent=dict(id=1, name='总控', job='安排工作', instructions='必须保留所有身份约束'*900,
                          kind='controller', revision=3, device_id=8),
               task=dict(id=4, title='重复标题', description='重复正文'*300, conversation_id=7),
               team=[dict(id=2, name='工程师')], experiences=[], conversations=[dict(purpose='不需要的路由候选'*1000)],
               attachments=[dict(id=9, original_filename='测试.png')])
    compact = execution_identity(ctx)
    assert compact['agent']['instructions'] == ctx['agent']['instructions']
    assert compact['attachments'] == ctx['attachments']
    assert '重复正文' not in json.dumps(compact, ensure_ascii=False)
    assert '不需要的路由候选' not in json.dumps(compact, ensure_ascii=False)
    assert compact['work'] == dict(id=4, conversation_id=7)


def test_history_is_bounded_and_keeps_lookup_ids():
    rows = [dict(id=i, title='任务', status='done', executor='工程师', result_summary='长结果'*2000) for i in range(20)]
    history = room_history(rows)
    assert len(history) == 6
    assert history[0]['id'] == 0 and history[0]['excerpted']
    assert len(json.dumps(history, ensure_ascii=False, separators=(',', ':'))) <= 5050


def test_machine_name_is_public_capability_not_arbitrary_settings():
    result = normalize_device_capabilities(dict(machine_name='BYBC-YOLO', token='private', api_key='private'))
    assert result['machine_name'] == 'BYBC-YOLO'
    assert 'private' not in json.dumps(result)
    assert 'machine_name' not in normalize_device_capabilities(dict(machine_name='bad\nname'))


def test_heartbeat_updates_only_authenticated_device_without_claiming_work(client, repo):
    from test_agents import token_headers
    device = repo.create_device(label='BYBC-YOLO heartbeat test')
    other = repo.create_device(label='BYBC-PRO heartbeat test')
    project = repo.create_project(slug='heartbeat-test', name='Heartbeat', created_by_device_id=device['id'])
    task = repo.create_task(project['id'], 'Do not claim on heartbeat', assigned_device_id=device['id'])
    assert client.post('/api/device/heartbeat').status_code == 401
    response = client.post('/api/device/heartbeat', headers=token_headers(repo, device))
    assert response.json() == {'ok': True, 'device_id': device['id']}
    assert repo.get_device(device['id'])['last_agent_seen_at']
    assert repo.get_device(other['id'])['last_agent_seen_at'] is None
    assert repo.get_task(task['id'])['status'] == 'open'


def test_cancelled_upload_close_does_not_leave_an_untracked_file(settings):
    import asyncio
    from types import SimpleNamespace
    import pytest
    from codex_hub.main import store_attachment_file

    class Upload:
        filename = 'cancelled.txt'
        content_type = 'text/plain'
        read_once = False

        async def read(self, size):
            if self.read_once:
                return b''
            self.read_once = True
            return b'already written'

        async def close(self):
            raise asyncio.CancelledError()

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(store_attachment_file(request, {'slug':'cancellation-test'}, Upload()))
    assert not [path for path in settings.files_dir.rglob('*') if path.is_file()]


def test_prompt_dedup_only_removes_original_dispatch_not_a_later_repeated_user_instruction():
    from codex_hub.prompts import build_task_prompt_markdown
    text = 'LATER-USER-INSTRUCTION-MUST-REMAIN'
    entry = dict(author_type='user', source_label='You', kind='message', created_at='2026-10-27', body_markdown=text)
    prompt = build_task_prompt_markdown(project=dict(name='Test', slug='test-project'), control=None,
        task=dict(id=1, title='Title', status='open', priority='normal', description=text, assigned_agent_id=1),
        entries=[dict(entry, metadata_json=json.dumps(dict(dispatch_id=1, task_ids=[1]))), entry], attachments=[])
    assert prompt.count(text) == 2
