import json
from types import SimpleNamespace

import pytest

from codex_bridge.agent_runtime import AgentDecisionError
from codex_bridge.model_allocator import allocate_task_model, decide_model, validate_model_plan
from codex_hub.model_policy import save_policy
from test_model_policy import setup_team
from test_agents import token_headers


def test_controller_chat_real_model_plan_api_skips_paid_decider(client, repo, settings, tmp_path):
    project, device, store, controller, worker, models = setup_team(repo, settings)
    task = repo.create_task(project['id'], '你好，现在进度怎么样了？', assigned_agent_id=controller['id'])
    headers = token_headers(repo, device)
    methods = []
    class APIClient:
        hub = 'https://hub.example/hub'
        def _json(self, method, path, data=None):
            methods.append(method)
            response = client.request(method, path, headers=headers, json=data)
            assert response.status_code == 200, response.text
            return response.json()
    def forbidden_decider(*args):
        raise AssertionError('ordinary controller chat must not start another paid model planner')
    config = SimpleNamespace(codex_exec_output_dir=tmp_path)
    first = allocate_task_model(config, APIClient(), project['slug'], task['id'], decider=forbidden_decider)
    repeated = allocate_task_model(config, APIClient(), project['slug'], task['id'], decider=forbidden_decider)
    assert first == repeated
    assert first['model'] == 'gpt-6-astra' and first['reasoning_effort'] == 'medium'
    assert methods == ['GET', 'GET']


def test_each_task_can_receive_different_controller_model_and_retry_is_stable(client, repo, settings):
    project, device, store, controller, worker, models = setup_team(repo, settings)
    headers = token_headers(repo, device)
    choices = [('小范围样式调整', 'gpt-5.6-sol', 'high'), ('排查跨设备运行竞态', 'gpt-6-sol', 'high')]
    for title, model, effort in choices:
        task = repo.create_task(project['id'], title, assigned_agent_id=worker['id'])
        path = f'/api/projects/{project["slug"]}/tasks/{task["id"]}/model-plan'
        context = client.get(path, headers=headers).json()
        assert context['selection'] is None and context['task']['title'] == title
        payload = {'model': model, 'reasoning_effort': effort, 'reason': title + '需要相应能力'}
        chosen = client.post(path, headers=headers, json=payload)
        assert chosen.status_code == 200, chosen.text
        selection = chosen.json()['selection']
        assert selection['source'] == 'controller_task_plan' and selection['model'] == model
        assert client.post(path, headers=headers, json=payload).json()['selection'] == selection
        assert client.get(path, headers=headers).json()['selection'] == selection
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_task_model_choices').fetchone()[0] == 2


def test_user_override_skips_paid_planner_and_prevents_task_plan_override(client, repo, settings):
    project, device, store, controller, worker, models = setup_team(repo, settings)
    save_policy(repo.connection, worker, settings.data_dir, mode='manual', model='gpt-5.6-sol', reasoning_effort='high')
    task = repo.create_task(project['id'], '按用户设置', assigned_agent_id=worker['id'])
    path = f'/api/projects/{project["slug"]}/tasks/{task["id"]}/model-plan'
    headers = token_headers(repo, device)
    result = client.get(path, headers=headers).json()['selection']
    assert result['source'] == 'user' and result['model'] == 'gpt-5.6-sol'
    assert client.post(path, headers=headers, json={'model': 'gpt-6-sol', 'reasoning_effort': 'high', 'reason': 'override'}).json()['selection'] == result


def test_model_plan_rejects_unknown_model_cancelled_task_and_unrelated_device(client, repo, settings):
    project, device, store, controller, worker, models = setup_team(repo, settings)
    task = repo.create_task(project['id'], '验证分配', assigned_agent_id=worker['id'])
    path = f'/api/projects/{project["slug"]}/tasks/{task["id"]}/model-plan'
    headers = token_headers(repo, device)
    payload = {'model': 'imaginary-model', 'reasoning_effort': 'high', 'reason': 'unsupported'}
    assert client.post(path, headers=headers, json=payload).status_code == 422
    other = repo.create_device('Other executor')
    repo.add_project_member(project['id'], other['id'], 'write')
    assert client.get(path, headers=token_headers(repo, other)).status_code == 403
    repo.update_task_status(task['id'], 'cancelled', 'Cancelled before planning')
    payload['model'] = 'gpt-6-sol'
    assert client.post(path, headers=headers, json=payload).status_code == 409


def test_successful_plan_is_cached_when_post_ack_is_lost(tmp_path):
    models = [{'model': 'gpt-6-sol', 'supportedReasoningEfforts': [{'reasoningEffort': 'medium'}]}]
    context = {'selection': None, 'models': models, 'task': {'id': 1}}
    decision = {'model': 'gpt-6-sol', 'reasoning_effort': 'medium', 'reason': '普通实现'}
    class Client:
        hub = 'https://hub.example/hub'
        attempts = 0
        def _json(self, method, path, data=None):
            if method == 'GET':
                return context
            self.attempts += 1
            if self.attempts == 1:
                raise OSError('network lost')
            return {'selection': data}
    client = Client()
    config = SimpleNamespace(codex_exec_output_dir=tmp_path)
    calls = []
    def decider(*args):
        calls.append(1)
        return decision
    with pytest.raises(OSError):
        allocate_task_model(config, client, 'example-project', 1, decider=decider)
    assert allocate_task_model(config, client, 'example-project', 1, decider=decider) == decision
    assert len(calls) == 1
    context['selection'] = decision
    assert allocate_task_model(config, client, 'example-project', 1, decider=decider) == decision
    assert len(calls) == 1


def test_planner_is_read_only_ephemeral_and_does_not_receive_project_path(tmp_path):
    cli = tmp_path / 'codex.exe'
    cli.touch()
    models = [{'model': 'gpt-6-sol', 'supportedReasoningEfforts': [{'reasoningEffort': 'medium'}]}]
    choice = {'model': 'gpt-6-sol', 'reasoning_effort': 'medium', 'reason': '普通实现'}
    class Process:
        @staticmethod
        def run(command, **kwargs):
            assert '--ignore-user-config' in command and '--ephemeral' in command
            assert command[command.index('--sandbox') + 1] == 'read-only'
            assert str(tmp_path) != command[command.index('--cd') + 1]
            from pathlib import Path
            Path(command[command.index('--output-last-message') + 1]).write_text(json.dumps(choice), encoding='utf-8')
            return SimpleNamespace(returncode=0)
    assert decide_model(SimpleNamespace(codex_cli_path=cli, backend_options={}), {'models': models}, Process) == choice
    with pytest.raises(AgentDecisionError):
        validate_model_plan({**choice, 'model': 'not-reported'}, models)
