import pytest

from codex_hub.agents import AgentStore
from codex_hub.device_capabilities import save_device_capabilities
from codex_hub.execution_leases import ExecutionStore
from codex_hub.model_policy import (agent_policy, ensure_task_choice, save_policy, select_model,
                                    task_choice, validate_choice)


def setup_team(repo, settings):
    device = repo.create_device('Model test computer')
    project = repo.create_project('model-team', 'Model team', created_by_device_id=device['id'])
    store = AgentStore(repo.connection)
    controller = store.create(project['id'], '总控', '分工', kind='controller', device_id=device['id'])
    worker = store.create(project['id'], '工程师', '维护', device_id=device['id'])
    models = [{'model': model, 'displayName': model,
               'supportedReasoningEfforts': [{'reasoningEffort': 'medium'}, {'reasoningEffort': 'high'}],
               'defaultReasoningEffort': 'medium'} for model in ['gpt-5.6-sol', 'gpt-6-sol', 'gpt-6-astra']]
    save_device_capabilities(settings.data_dir, device['id'], {'codex': {'models': models}})
    return project, device, store, controller, worker, models


def test_manual_choice_wins_over_controller_and_has_audit(repo, settings):
    project, device, store, controller, worker, models = setup_team(repo, settings)
    policy = save_policy(repo.connection, worker, settings.data_dir, mode='manual',
                        model='gpt-5.6-sol', reasoning_effort='high', actor='user')
    selected = select_model(repo.connection, worker, settings.data_dir,
        {'model': 'gpt-6-astra', 'reasoning_effort': 'high', 'reason': 'complex'})
    assert selected['model'] == 'gpt-5.6-sol' and selected['source'] == 'user'
    with pytest.raises(ValueError, match='不能覆盖'):
        save_policy(repo.connection, worker, settings.data_dir, mode='controller',
                    model='gpt-6-sol', reasoning_effort='medium', actor='controller-task:1')
    assert agent_policy(repo.connection, worker['id'])['revision'] == policy['revision']
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_model_policy_events').fetchone()[0] == 1


def test_short_followup_uses_medium_unless_user_explicitly_overrides(repo, settings):
    project, device, store, controller, worker, models = setup_team(repo, settings)
    task = {'description': '你还需要什么吗', 'controller_read_only': 1}
    choice = select_model(repo.connection, controller, settings.data_dir, task=task)
    assert choice['model'] == 'gpt-6-astra' and choice['reasoning_effort'] == 'medium'
    save_policy(repo.connection, controller, settings.data_dir, mode='manual',
                model='gpt-6-astra', reasoning_effort='high', actor='user')
    choice = select_model(repo.connection, controller, settings.data_dir, task=task)
    assert choice['source'] == 'user' and choice['reasoning_effort'] == 'high'


def test_controller_choice_and_default_preference_use_reported_capabilities(repo, settings):
    project, device, store, controller, worker, models = setup_team(repo, settings)
    assert select_model(repo.connection, worker, settings.data_dir)['model'] == 'gpt-6-sol'
    selected = select_model(repo.connection, worker, settings.data_dir,
        {'model': 'gpt-6-sol', 'reasoning_effort': 'high', 'reason': '跨设备恢复逻辑需要审查'})
    assert selected['source'] == 'controller' and selected['reasoning_effort'] == 'high'
    for value in [{'model': 'nonexistent-model', 'reasoning_effort': 'high'},
                  {'model': 'gpt-6-sol', 'reasoning_effort': 'ultra'}]:
        with pytest.raises(ValueError):
            validate_choice(value, models)


def test_task_snapshot_does_not_change_after_policy_or_binding_edit(repo, settings):
    project, device, store, controller, worker, models = setup_team(repo, settings)
    task = repo.create_task(project['id'], '冻结模型', assigned_agent_id=worker['id'])
    original = ensure_task_choice(repo, task, settings.data_dir)
    save_policy(repo.connection, worker, settings.data_dir, mode='manual',
                model='gpt-5.6-sol', reasoning_effort='high')
    assert ensure_task_choice(repo, task, settings.data_dir) == original
    next_task = repo.create_task(project['id'], '下一个任务', assigned_agent_id=worker['id'])
    assert ensure_task_choice(repo, next_task, settings.data_dir)['model'] == 'gpt-5.6-sol'


def test_delegate_preserves_model_snapshot_and_retry_does_not_duplicate(repo, settings):
    project, device, store, controller, worker, models = setup_team(repo, settings)
    source = repo.create_task(project['id'], '总控安排', assigned_agent_id=controller['id'])
    source = store.route(source['id'], {'action': 'new', 'title': '协作', 'reason': 'test'}, 'codex_cli')
    run = ExecutionStore(repo).acquire(source['id'], device['id'], 'model-execution-key-001')['execution']
    ExecutionStore(repo).start(source['id'], device['id'], run['generation'], run['run_key'])
    selection = select_model(repo.connection, worker, settings.data_dir,
        {'model': 'gpt-5.6-sol', 'reasoning_effort': 'high', 'reason': '范围明确的维护'})
    args = dict(agent_ids=[worker['id']], title='检查反馈', description='复核问题', request_key='model-delegate-001',
        conversation_id=source['conversation_id'], source_task_id=source['id'], device_id=device['id'],
        execution=run, model_selection=selection)
    first = store.dispatch(repo, project['id'], **args)
    second = store.dispatch(repo, project['id'], **args)
    assert first['tasks'][0]['id'] == second['tasks'][0]['id']
    assert task_choice(repo.connection, first['tasks'][0]['id']) == selection


def test_unreported_default_is_explicit_not_an_invented_model(repo, settings):
    project, device, store, controller, worker, models = setup_team(repo, settings)
    save_device_capabilities(settings.data_dir, device['id'], {'codex': {'models': []}})
    selection = select_model(repo.connection, worker, settings.data_dir)
    assert selection['source'] == 'device_default' and selection['model'] == ''
    with pytest.raises(ValueError, match='尚未上报'):
        save_policy(repo.connection, worker, settings.data_dir, mode='manual',
                    model='gpt-6-sol', reasoning_effort='medium')


def test_user_can_release_override_back_to_controller(repo, settings):
    project, device, store, controller, worker, models = setup_team(repo, settings)
    save_policy(repo.connection, worker, settings.data_dir, mode='manual',
                model='gpt-5.6-sol', reasoning_effort='high')
    save_policy(repo.connection, worker, settings.data_dir, mode='controller')
    saved = save_policy(repo.connection, worker, settings.data_dir, mode='controller',
                model='gpt-6-sol', reasoning_effort='high', reason='需要多场景验证', actor='controller-task:1')
    assert saved['revision'] == 3
    assert select_model(repo.connection, worker, settings.data_dir)['reasoning_effort'] == 'high'
