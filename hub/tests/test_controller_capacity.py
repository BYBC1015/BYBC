import pytest

from codex_hub.controller_capacity import WAIT_MESSAGE, runnable_inbox
from codex_hub.device_capabilities import save_device_capabilities
from codex_hub.execution_leases import ExecutionConflict, ExecutionStore
from codex_hub.model_policy import freeze_choice, reserve_policy, save_policy, task_choice
from codex_hub.model_services import ModelServices
from test_execution_leases import lease_task
from test_native_mobile_api import PROJECT, native_team, send, upload


CLI = {'schema_version': 1, 'reason': 'quota_exhausted', 'code': 'usage_limit_reached', 'returncode': 1,
       'event_types': ['thread.started', 'turn.started', 'turn.failed'], 'no_work_observed': True,
       'stream_sha256': 'a' * 64}
HTTP = {'schema_version': 2, 'reason': 'quota_exhausted', 'code': 'insufficient_balance', 'http_status': 402,
        'request_index': 1, 'no_work_observed': True}


@pytest.fixture
def hub(lease_task, repo, settings):
    d = lease_task
    d['clock'] = [1_800_000_000]
    d['device_id'] = d['device']['id']
    d['store'] = ExecutionStore(repo, data_dir=settings.data_dir, clock=lambda: d['clock'][0])
    d['repo'], d['data_dir'] = repo, settings.data_dir
    save_device_capabilities(settings.data_dir, d['device_id'], {'model_services': True})
    d['services'] = ModelServices(repo.connection, settings.data_dir)
    return d


def task(d, route='chat'):
    repo = d['repo']
    t = repo.create_task(d['project']['id'], '在已有要求上追加工作', assigned_agent_id=d['controller']['id'])
    d['agents'].route(t['id'], {'action':'new','title':'独立测试','reason':'隔离'}, 'codex_cli')
    with repo.connection:
        repo.connection.execute('UPDATE tasks SET execution_route=? WHERE id=?', (route, t['id']))
        freeze_choice(repo.connection, t['id'], {'model':'gpt-6-astra','reasoning_effort':'high','source':'default_policy'})
    return t['id']


def reserve(d, **kwargs):
    service_id = d['services'].save(d['project']['id'], {'name':'本机测试', 'kind':'ollama',
        'base_url':'http://127.0.0.1:11434/v1', 'model':'tools-test', 'supports_tools':True,
        'device_id': d['device_id'], **kwargs})
    name = f"hub-service/{service_id}"
    save_policy(d['repo'].connection, d['controller'], d['data_dir'], mode='controller', reserve=name+'@1')
    return service_id, name


def run(d, tid):
    n = d['repo'].connection.execute('SELECT COUNT(*) FROM hub_task_executions WHERE task_id=?', (tid,)).fetchone()[0]
    prepared = d['store'].acquire(tid, d['device_id'], f'capacity-generation-{n+1:04d}')
    key = prepared['execution']['run_key']
    d['store'].start(tid, d['device_id'], n+1, key)
    return n+1, key


def reject(d, tid, generation, proof=CLI, **extra):
    return d['store'].complete(tid, d['device_id'], *generation, status='blocked', summary='模型拒绝',
        body='未交付', reflection={}, provider_rejection=proof, **extra)


def visible(d):
    rows = d['repo'].list_inbox_tasks_for_device(d['device_id'])
    return [t['id'] for t in runnable_inbox(d['repo'], d['device_id'], d['data_dir'], rows, d['clock'][0])]


def test_zero_keys_repeated_receipt_keeps_same_task_and_model_and_recovers(hub):
    d = hub
    tid = task(d)
    g = run(d, tid)
    before = d['repo'].get_task(tid)
    response = reject(d, tid, g)
    assert response['capacity_deferred'] and response['execution']['state']=='yielded'
    assert response['task']['result_summary'] == WAIT_MESSAGE
    assert response['task']['conversation_id'] == before['conversation_id']
    assert reject(d, tid, g)['replayed']
    assert tid not in visible(d)
    assert d['task']['id'] in visible(d)  # An independent engineer on the same device.
    assert d['store'].acquire(tid, d['device_id'], 'another-safe-request')['execution'] is None
    assert d['repo'].connection.execute('SELECT COUNT(*) FROM entries WHERE task_id=?', (tid,)).fetchone()[0] == 0
    d['clock'][0] += 601
    assert tid in visible(d)
    g2 = run(d, tid)
    late_ack = reject(d, tid, g)
    assert late_ack['replayed'] and late_ack['execution']['generation'] == g[0]
    assert late_ack['task']['status'] == 'in_progress'
    with pytest.raises(ExecutionConflict):
        reject(d, tid, g, {**CLI, 'stream_sha256':'b' * 64})
    d['store'].complete(tid, d['device_id'], *g2, status='review', summary='已完成', body='结果', reflection={})
    assert d['repo'].connection.execute('SELECT COUNT(*) FROM hub_controller_cooldowns').fetchone()[0] == 0


def test_authorized_standby_then_both_empty_no_loop_and_primary_trial_is_exclusive(hub):
    d = hub
    tid = task(d)
    first = run(d, tid)
    reject(d, tid, first)
    _, name = reserve(d)
    assert tid in visible(d)
    second = run(d, tid)
    assert task_choice(d['repo'].connection, tid)['model'] == name
    assert reject(d, tid, second, HTTP)['capacity_deferred']
    assert tid not in visible(d)
    other = task(d)
    assert d['store'].acquire(other, d['device_id'], 'capacity-other-0001')['execution'] is None
    assert other not in visible(d)
    d['clock'][0] += 601
    third = run(d, tid)
    assert task_choice(d['repo'].connection, tid)['model'] == 'gpt-6-astra'
    # While the primary is being tried, the *other* known-failed provider may
    # also be tried once. Neither endpoint may take a burst of queued work.
    other_gen = run(d, other)
    assert task_choice(d['repo'].connection, other)['model'] == name
    final = task(d)
    assert d['store'].acquire(final, d['device_id'], 'capacity-final-0001')['execution'] is None
    assert len({third, other_gen}) == 2


@pytest.mark.parametrize('why', ['old_worker','transport','tokens','assistant','delegate','wrong_provider','cancelled'])
def test_uncertain_or_contradictory_outcome_never_reopens(hub, why):
    d = hub
    tid = task(d)
    gen = run(d, tid)
    db = d['repo'].connection
    run_id = db.execute('SELECT id FROM hub_task_executions WHERE task_id=?', (tid,)).fetchone()[0]
    if why == 'cancelled':
        d['repo'].update_task_status(tid, 'cancelled')
        with pytest.raises(ExecutionConflict):
            reject(d, tid, gen)
        return
    with db:
        if why == 'assistant':
            db.execute("INSERT INTO hub_execution_progress VALUES (?,?,1,'assistant','hash','','已经做了一部分',0)", (run_id,tid))
        if why == 'delegate':
            t = d['repo'].get_task(tid)
            db.execute("INSERT INTO hub_agent_dispatches(project_id,request_key,payload_hash,conversation_id,created_at) VALUES (?,?,?,?,?)",
                       (t['project_id'],f'delegate:{tid}:previous','h',t['conversation_id'],'t'))
    proof = {'old_worker':None,'transport':{**CLI,'code':'timeout'},'wrong_provider':HTTP}.get(why, CLI)
    extra = {'token_usage':{'input_tokens':20,'cached_input_tokens':0,'output_tokens':2}} if why=='tokens' else {}
    response = reject(d, tid, gen, proof, **extra)
    assert response['execution']['state']=='completed' and response['task']['status']=='blocked'
    assert not db.execute('SELECT 1 FROM hub_controller_cooldowns').fetchone()


def test_updated_service_and_project_work_require_appropriate_connection(hub):
    d = hub
    sid, name = reserve(d)
    tid = task(d, route='project')
    reject(d, tid, run(d, tid))
    assert tid not in visible(d)
    d['services'].save(d['project']['id'], {'model':'changed-destination'}, sid)
    chat = task(d)
    assert d['store'].acquire(chat, d['device_id'], 'capacity-unchanged-01')['execution'] is None
    with pytest.raises(ValueError):
        save_policy(d['repo'].connection, d['controller'], d['data_dir'], mode='controller', reserve=name+'@1')
    save_policy(d['repo'].connection, d['controller'], d['data_dir'], mode='controller')
    assert reserve_policy(d['repo'].connection, d['controller']['id'])['service_revision']==1
    save_policy(d['repo'].connection, d['controller'], d['data_dir'], mode='controller', reserve=name+'@2')
    assert chat in visible(d) and tid not in visible(d)
    save_policy(d['repo'].connection, d['controller'], d['data_dir'], mode='controller', reserve='')
    assert chat not in visible(d)


def test_user_can_send_followups_and_attachments_while_controller_offline(client, native_team, repo):
    import time
    data = native_team
    now = time.time()
    device = data['controller']['device_id']
    with repo.connection:
        repo.connection.execute("INSERT INTO hub_controller_cooldowns VALUES (?,'codex-cli',?,?,1)",
                                (device, now, now + 600))
    fields = {'agent_ids':[data['controller']['id']], 'request_key':'quota-attachment-0001'}
    first = upload(client, data, **fields)
    assert first.status_code == 200, first.text
    replayed = upload(client, data, **fields)
    assert replayed.json()['task_ids'] == first.json()['task_ids'] and not replayed.json()['created']
    second = send(client, data, agent_ids=fields['agent_ids'], request_key='quota-followup-0002')
    assert second.status_code == 200
    tid, = first.json()['task_ids']
    assert repo.get_task(tid)['result_summary'] == WAIT_MESSAGE
    assert len(repo.list_task_attachments(data['project']['id'], tid)) == 1
    view = client.get(PROJECT+'/workspace', params={'conversation_id':data['conversation_id']})
    assert view.status_code == 200
    assert '已收到 · 等待模型额度恢复' in view.text
    assert client.get(PROJECT+'/library').status_code == 200
    assert client.get('/api/projects/workspace-team/settings').status_code == 200


def test_late_receipt_from_old_worker_after_cancel_cannot_create_work(hub):
    d = hub
    tid = task(d)
    gen = run(d, tid)
    reject(d, tid, gen)
    d['repo'].update_task_status(tid, 'cancelled')
    assert reject(d, tid, gen)['task']['status'] == 'cancelled'
    assert tid not in visible(d)


def test_expired_preparation_is_not_lost_if_capacity_check_holds_new_grant(hub):
    d = hub
    tid = task(d)
    store = d['store']
    store.acquire(tid, d['device_id'], 'prepared-before-quota')
    d['clock'][0] += 160
    now = d['clock'][0]
    with d['repo'].connection:
        d['repo'].connection.execute("INSERT INTO hub_controller_cooldowns VALUES (?,'codex-cli',?,?,1)",
                                    (d['device_id'], now, now+600))
    assert store.acquire(tid, d['device_id'], 'different-run-after-expiry')['execution'] is None
    d['clock'][0] += 601
    assert store.acquire(tid, d['device_id'], 'different-run-after-expiry')['execution']['generation'] == 2


def test_text_only_reserve_never_receives_attached_image(client, native_team, repo, settings):
    import io
    import time
    from PIL import Image
    data = native_team
    device = data['controller']['device_id']
    save_device_capabilities(settings.data_dir, device, {'model_services':True})
    services = ModelServices(repo.connection, settings.data_dir)
    sid = services.save(data['project']['id'], {'name':'测试文字模型', 'kind':'ollama',
        'base_url':'http://127.0.0.1:11434/v1','model':'text-only','supports_tools':True,
        'supports_images':False,'device_id':device})
    save_policy(repo.connection, data['controller'], settings.data_dir, mode='controller', reserve=f'hub-service/{sid}@1')
    now = time.time()
    with repo.connection:
        repo.connection.execute("INSERT INTO hub_controller_cooldowns VALUES (?,'codex-cli',?,?,1)", (device,now,now+600))
    buf = io.BytesIO()
    Image.new('RGB', (4,4)).save(buf, format='PNG')
    fields = {'agent_ids':[data['controller']['id']], 'request_key':'quota-image-native-01'}
    saved = upload(client, data, files=[('attachments', ('reference.png', buf.getvalue(), 'image/png'))], **fields)
    assert saved.status_code == 200
    tid, = saved.json()['task_ids']
    assert repo.get_task(tid)['result_summary'] == WAIT_MESSAGE
    assert tid not in [t['id'] for t in runnable_inbox(repo, device, settings.data_dir, repo.list_inbox_tasks_for_device(device), now)]


def test_removed_primary_api_does_not_reactivate_its_old_credentials(hub):
    d = hub
    sid, name = reserve(d)
    tid = task(d)
    import json
    with d['repo'].connection:
        d['repo'].connection.execute('UPDATE hub_task_model_choices SET selection_json=? WHERE task_id=?',
            (json.dumps({'model':name, 'service_revision':1, 'source':'user', 'reasoning_effort':'none'}), tid))
    reject(d, tid, run(d, tid), HTTP)
    d['services'].delete(d['project']['id'], sid)
    d['clock'][0] += 601
    assert tid not in visible(d)
    assert d['store'].acquire(tid, d['device_id'], 'obsolete-service-0001')['execution'] is None
