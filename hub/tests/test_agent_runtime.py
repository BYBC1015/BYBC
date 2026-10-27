import json
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest

from codex_bridge.agent_runtime import (
    AgentDecisionError, CodexAgentDecisionRunner, extract_reflection, validate_decision, run_agent_request,
)
from codex_bridge.client import HubClient, HubClientError
from codex_bridge.config import BridgeConfig
from codex_bridge.runner import BridgeRunner
from test_codex_bridge import FakeExecutor, FakeHubClient, FakeLauncher


REFLECTION = {"summary": "已生成预览", "worked": "先用样本验证尺寸", "failed": "", "next_time": "同类任务先核对画幅"}


class AgentClient(FakeHubClient):
    def __init__(self, *, existing=False, fallback_fails=False):
        super().__init__({"device": {"label": "Codex"}, "tasks": [{"id": 42,
            "project_slug": "agent-test", "assigned_agent_id": 7, "status": "open"}], "mentions": []})
        self.context = {"agent": {"id": 7, "name": "设计师"},
            "task": {"id": 42, "conversation_id": 12 if existing else None},
            "experiences": [], "conversations": [{"id": 12, "status": "active", "title": "预览"}]}
        self.routes = []
        self.fallback_calls = 0
        self.fallback_fails = fallback_fails
        self.reflections = []
        self.fail_result_once = False
        self.fail_entry = False

    def get_agent_context(self, slug, task_id):
        return self.context

    def route_agent_task(self, slug, task_id, decision):
        self.routes.append(decision)
        self.task_prompt = "Fresh identity + experience + conversation context"
        return {"task": {"conversation_id": 12}}

    def fallback_agent_route(self, slug, task_id):
        self.fallback_calls += 1
        if self.fallback_fails:
            raise HubClientError("DeepSeek unavailable")
        self.task_prompt = "DeepSeek decision + fresh conversation context"
        return {"task": {"conversation_id": 13}}

    def update_task_status(self, slug, task_id, status, result_summary="", reflection=None):
        if reflection is not None and self.fail_result_once:
            self.fail_result_once = False
            raise HubClientError("temporary result timeout")
        self.inbox["tasks"][0]["status"] = status
        super().update_task_status(slug, task_id, status, result_summary)
        if reflection is not None:
            self.reflections.append(reflection)

    def post_task_entry(self, *args, **kwargs):
        super().post_task_entry(*args, **kwargs)
        if self.fail_entry:
            raise HubClientError("unknown acknowledgement")


class Decider:
    def __init__(self, *, fails=False):
        self.calls = []
        self.fails = fails

    def decide(self, context):
        self.calls.append(context)
        if self.fails:
            raise AgentDecisionError("CLI offline")
        return {"action": "reuse", "conversation_id": 12, "title": "", "reason": "同一预览任务"}


def config_for(tmp_path):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return BridgeConfig(hub="https://hub.example/hub", token="test", prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json", projects={"agent-test": project},
        codex_exec_output_dir=tmp_path / "outputs", auto_execute_codex=True,
        ignore_existing_on_first_run=False)


def build_runner(tmp_path, client=None, decider=None, executor=None):
    client = client or AgentClient()
    decider = decider or Decider()
    executor = executor or FakeExecutor(summary="预览完成。\n<hub-reflection>" + json.dumps(REFLECTION) + "</hub-reflection>")
    runner = BridgeRunner(config_for(tmp_path), client=client, executor=executor,
                         launcher=FakeLauncher(), agent_decider=decider)
    return runner, client, decider, executor


def test_agent_routes_before_work_and_saves_structured_experience(tmp_path):
    runner, client, decider, executor = build_runner(tmp_path)
    result = runner.process_once()
    assert len(decider.calls) == len(client.routes) == len(executor.calls) == 1
    assert "Fresh identity" in executor.calls[0][3]
    assert client.reflections == [REFLECTION]
    assert client.task_status_updates[-1][2] == "review"
    assert "<hub-reflection>" not in client.task_entries[0][2]
    assert result.pending_tasks == []
    runner.process_once()
    assert len(executor.calls) == 1


def test_existing_work_conversation_is_not_decided_twice(tmp_path):
    runner, client, decider, executor = build_runner(tmp_path, client=AgentClient(existing=True))
    runner.process_once()
    assert decider.calls == []
    assert len(executor.calls) == 1


def test_cli_decision_failure_falls_back_before_work(tmp_path):
    runner, client, decider, executor = build_runner(tmp_path, decider=Decider(fails=True))
    runner.process_once()
    assert client.fallback_calls == 1
    assert "DeepSeek decision" in executor.calls[0][3]


def test_two_decision_failures_block_without_any_work_or_poll_loop(tmp_path):
    runner, client, decider, executor = build_runner(tmp_path,
        client=AgentClient(fallback_fails=True), decider=Decider(fails=True))
    runner.process_once()
    runner.process_once()
    assert executor.calls == []
    assert client.fallback_calls == 1
    assert client.task_status_updates[-1][2] == "blocked"
    assert "未运行工作进程" in client.reflections[0]["failed"]
    client.inbox["tasks"][0]["status"] = "open"
    client.fallback_fails = False
    runner.process_once()
    assert len(executor.calls) == 1


def test_result_receipt_survives_restart_and_does_not_execute_twice(tmp_path):
    runner, client, decider, executor = build_runner(tmp_path)
    client.fail_result_once = True
    with pytest.raises(HubClientError):
        runner.process_once()
    assert runner.state.get_task_delivery("agent-test", 42)["stage"] == "agent_result"
    restarted = BridgeRunner(runner.config, client=client, executor=executor,
                            launcher=FakeLauncher(), agent_decider=decider)
    restarted.process_once()
    assert len(executor.calls) == 1
    assert client.reflections == [REFLECTION]
    assert len(client.task_entries) == 1


def test_decision_failure_report_retries_without_replanning(tmp_path):
    runner, client, decider, executor = build_runner(tmp_path,
        client=AgentClient(fallback_fails=True), decider=Decider(fails=True))
    client.fail_result_once = True
    with pytest.raises(HubClientError):
        runner.process_once()
    assert not runner.state.get_task_delivery("agent-test", 42)["status_reported"]
    runner.process_once()
    assert len(decider.calls) == client.fallback_calls == 1
    assert not executor.calls
    assert len(client.reflections) == 1
    assert client.task_status_updates[-1][2] == "blocked"


def test_inflight_execution_is_never_restarted(tmp_path):
    runner, client, decider, executor = build_runner(tmp_path)
    runner.state.set_task_delivery("agent-test", 42, "agent_running")
    runner._save_state()
    runner.process_once()
    assert not executor.calls and not decider.calls and not client.claimed_tasks


def test_recovered_result_does_not_reopen_manually_finished_task(tmp_path):
    runner, client, decider, executor = build_runner(tmp_path)
    client.fail_result_once = True
    with pytest.raises(HubClientError):
        runner.process_once()
    client.inbox["tasks"][0]["status"] = "done"
    runner.process_once()
    assert len(executor.calls) == 1
    assert not client.reflections
    assert client.inbox["tasks"][0]["status"] == "done"
    assert not runner.state.get_task_delivery("agent-test", 42)


def test_missing_reflection_records_result_without_success_lesson(tmp_path):
    runner, client, _, executor = build_runner(tmp_path, executor=FakeExecutor(summary="Built preview"))
    result = runner.process_once()
    assert client.reflections[0]["worked"] == ""
    assert "缺少有效" in client.reflections[0]["failed"]
    assert any("待补充" in message for message in result.messages)


def test_failed_execution_is_blocked_and_never_reexecuted_through_fallback(tmp_path):
    runner, client, _, executor = build_runner(tmp_path, executor=FakeExecutor(ok=False, summary="Build failed"))
    runner.process_once()
    assert client.task_status_updates[-1][2] == "blocked"
    assert client.fallback_calls == 0
    assert client.reflections[0]["worked"] == ""


def test_successful_process_can_report_business_blocker(tmp_path):
    reflection = {**REFLECTION, "outcome": "blocked", "summary": "委派接口未返回任务编号"}
    executor = FakeExecutor(summary="受阻。<hub-reflection>" + json.dumps(reflection) + "</hub-reflection>")
    runner, client, _, _ = build_runner(tmp_path, executor=executor)
    runner.process_once()
    assert client.task_status_updates[-1][2] == "blocked"
    assert "状态：遇阻" in client.task_entries[0][2]


def test_invalid_outcome_does_not_change_default_status(tmp_path):
    reflection = {**REFLECTION, "outcome": "done"}
    executor = FakeExecutor(summary="完成。<hub-reflection>" + json.dumps(reflection) + "</hub-reflection>")
    runner, client, _, _ = build_runner(tmp_path, executor=executor)
    runner.process_once()
    assert client.task_status_updates[-1][2] == "review"


def test_failed_process_overrides_review_outcome():
    _, reflection, valid = extract_reflection("<hub-reflection>" + json.dumps({**REFLECTION, "outcome": "review"}) + "</hub-reflection>", ok=False)
    assert valid and reflection["outcome"] == "blocked"


def test_unknown_entry_acknowledgement_does_not_duplicate_result(tmp_path):
    runner, client, _, executor = build_runner(tmp_path)
    client.fail_entry = True
    runner.process_once()
    runner.process_once()
    assert len(client.task_entries) == 1
    assert len(executor.calls) == 1
    assert client.reflections == [REFLECTION]


@pytest.mark.parametrize("text", ["plain text", "<hub-reflection>{}</hub-reflection>",
    '<hub-reflection>{"summary":3}</hub-reflection>'])
def test_malformed_reflection_remains_unverified(text):
    _, reflection, valid = extract_reflection(text, ok=True)
    assert valid is False and reflection["worked"] == ""


def test_foreign_or_invalid_conversation_choice_rejected():
    context = {"conversations": [{"id": 2, "status": "active"}]}
    for choice in (99, "2", True):
        with pytest.raises(AgentDecisionError):
            validate_decision(json.dumps({"action": "reuse", "conversation_id": choice, "reason": "same", "title": ""}), context)


def test_planner_is_isolated_read_only_and_structured(tmp_path):
    config = config_for(tmp_path)
    config.codex_cli_path = tmp_path / "codex.cmd"
    config.codex_cli_path.write_text("placeholder", encoding="utf-8")
    config.backend_options = {"codex_model": "gpt-6-astra", "agent_network_access": True, "windows_sandbox": "elevated"}
    calls = []

    class Process:
        def run(self, command, **kwargs):
            calls.append((command, kwargs))
            output = Path(command[command.index("--output-last-message")+1])
            output.write_text(json.dumps({"action": "new", "conversation_id": None,
                                         "title": "Preview", "reason": "new topic"}), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

    decision = CodexAgentDecisionRunner(config, Process()).decide({"conversations": []})
    assert decision["action"] == "new"
    command, kwargs = calls[0]
    assert command[command.index("--sandbox")+1] == "read-only"
    assert "--ephemeral" in command and "--ignore-user-config" in command
    assert "--output-schema" in command
    assert command[command.index("--model")+1] == "gpt-6-astra"
    assert 'windows.sandbox="elevated"' in command
    assert "sandbox_workspace_write.network_access=true" not in command
    assert command[command.index("--cd")+1] != str(tmp_path / "project")
    assert kwargs["timeout"] == 120
    assert not Path(command[command.index("--cd")+1]).exists()


def test_client_serializes_reflection_and_routing_payloads():
    client = HubClient("https://hub.example", "test")
    calls = []
    client._json = lambda *args: calls.append(args) or {"ok": True}
    client.update_task_status("sample", 1, "review", "result", reflection=REFLECTION)
    assert calls[-1][2]["reflection"] == REFLECTION
    client.route_agent_task("sample", 1, {"action": "new"})
    assert calls[-1][2] == {"decision": {"action": "new"}, "engine": "codex_cli"}


def test_controller_helper_uses_binding_and_stable_request_key(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    requests = []
    monkeypatch.setattr("codex_bridge.config.load_config", lambda _: config)
    monkeypatch.setattr("codex_bridge.hub_reply.select_runtime_config", lambda value, binding: value)

    class Client:
        def __init__(self, hub, token, **kwargs):
            assert token == "test"

        def get_agent_context(self, slug, task_id):
            return {"agent": {"id": 5, "kind": "controller"}}

        def _json(self, *args):
            requests.append(args)
            return {"task": {"id": 10, "status": "open"}}

    monkeypatch.setattr("codex_bridge.client.HubClient", Client)
    source = tmp_path / "request.json"
    source.write_text(json.dumps({"action": "delegate", "assigned_agent_id": 9,
                                 "title": "预览", "description": "检验画幅"}), encoding="utf-8")
    args = SimpleNamespace(config=tmp_path / "config.json", binding_id="codex", project="sample-project",
                           task_id=3, request_file=source)
    delegated = run_agent_request(args)["task"]
    assert delegated["id"] == 10
    assert delegated["status"] == "open"
    run_agent_request(args)
    assert requests[0] == requests[1]
    assert requests[0][1] == "/api/projects/sample-project/tasks/3/delegate"
    assert len(requests[0][2]["request_key"]) == 64
    assert "token" not in json.dumps(requests)
    monkeypatch.setenv('CODEX_HUB_EXECUTION_TASK_ID', '3')
    monkeypatch.setenv('CODEX_HUB_EXECUTION_PROJECT', 'sample-project')
    monkeypatch.setenv('CODEX_HUB_EXECUTION_GENERATION', '4')
    monkeypatch.setenv('CODEX_HUB_EXECUTION_RUN_KEY', 'scoped-generation-key-123')
    source.write_text(json.dumps({'action':'delegate','assigned_agent_id':9,'title':'预览',
        'model_selection':{'model':'gpt-5.6-sol','reasoning_effort':'high','reason':'有限范围的明确工作'}}), encoding='utf-8')
    run_agent_request(args)
    assert requests[-1][2]['execution'] == {'generation':4,'run_key':'scoped-generation-key-123'}
    assert requests[-1][2]['model_selection']['model'] == 'gpt-5.6-sol'
    source.write_text(json.dumps({'action':'model_options','agent_id':9}), encoding='utf-8')
    run_agent_request(args)
    assert requests[-1] == ('GET','/api/projects/sample-project/agents/9/model-policy')
    source.write_text(json.dumps({'action':'delegate','work_type':'general_support','work_key':'translate-note-v1',
        'fallback_reason':'这是资料翻译工作，现有专业均不适用','title':'翻译资料'}),encoding='utf-8')
    run_agent_request(args)
    assert requests[-1][2]['assigned_agent_id'] is None
    assert requests[-1][2]['work_type']=='general_support'
    assert requests[-1][2]['work_key']=='translate-note-v1'
    assert requests[-1][2]['fallback_reason']=='这是资料翻译工作，现有专业均不适用'


def test_worker_helper_cannot_delegate(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    monkeypatch.setattr("codex_bridge.config.load_config", lambda _: config)
    monkeypatch.setattr("codex_bridge.hub_reply.select_runtime_config", lambda value, binding: value)

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def get_agent_context(self, slug, task_id):
            return {"agent": {"id": 5, "kind": "worker"}}

        def _json(self, *args):
            raise AssertionError("must not create work")

    monkeypatch.setattr("codex_bridge.client.HubClient", Client)
    source = tmp_path / "request.json"
    source.write_text(json.dumps({"action": "delegate", "assigned_agent_id": 9, "title": "test"}), encoding="utf-8")
    with pytest.raises(ValueError, match="仅总控"):
        run_agent_request(SimpleNamespace(config=None, binding_id=None, project="sample-project",
                                         task_id=3, request_file=source))


def test_dedicated_worker_prompt_uses_its_own_config(tmp_path, monkeypatch):
    dedicated = str(tmp_path / "dedicated-worker.json")
    monkeypatch.setenv("CODEX_BRIDGE_AGENT_CONFIG", dedicated)
    runner, _, _, _ = build_runner(tmp_path)
    instruction = runner._agent_tool_instructions("agent-test", 42)
    assert "--config '" + dedicated + "'" in instruction
    assert "token" in instruction  # prohibition only, never actual config token
    assert "--request-file" in instruction
