import subprocess
import pytest
import json

from codex_bridge.codex_exec import CodexExecRunner
from codex_bridge.config import BridgeConfig


class FakeProcessRunner:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.calls = []

    def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        output_path = command[command.index("--output-last-message") + 1]
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write("final result")
        return subprocess.CompletedProcess(
            command,
            self.returncode,
            stdout="stdout result",
            stderr="" if self.returncode == 0 else "stderr error",
        )


def test_codex_exec_runner_uses_stdin_prompt_and_writes_last_message(tmp_path):
    cli_path = tmp_path / "codex.cmd"
    cli_path.write_text("@echo off", encoding="utf-8")
    project_path = tmp_path / "project"
    project_path.mkdir()
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("# Prompt", encoding="utf-8")
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={"private-server-buildout": project_path},
        codex_cli_path=cli_path,
        codex_exec_output_dir=tmp_path / "results",
        codex_exec_sandbox="workspace-write",
        codex_exec_timeout_seconds=60,
    )
    process_runner = FakeProcessRunner()

    result = CodexExecRunner(config, process_runner).run(
        "private-server-buildout", project_path, prompt_path, "do the work"
    )

    command, kwargs = process_runner.calls[0]
    assert result.ok is True
    assert result.last_message == "final result"
    assert command[:5] == [
        str(cli_path),
        "--ask-for-approval",
        "never",
        "exec",
        "--ignore-user-config",
    ]
    assert command[-1] == "-"
    assert kwargs["input"] == "do the work"
    assert "--cd" in command
    assert str(project_path.resolve()) in command
    assert "--sandbox" in command
    assert "workspace-write" in command


def test_codex_exec_runner_reports_missing_cli_path(tmp_path):
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={},
        codex_cli_path=None,
        codex_exec_output_dir=tmp_path / "results",
    )

    result = CodexExecRunner(config, FakeProcessRunner()).run(
        "private-server-buildout", tmp_path, tmp_path / "prompt.md", "do the work"
    )

    assert result.ok is False
    assert "not configured" in result.summary


def test_codex_exec_runner_applies_message_model_and_reasoning_overrides(tmp_path):
    cli_path = tmp_path / "codex.cmd"
    cli_path.write_text("@echo off", encoding="utf-8")
    project_path = tmp_path / "project"
    project_path.mkdir()
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("# Prompt", encoding="utf-8")
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={"private-server-buildout": project_path},
        codex_cli_path=cli_path,
        codex_exec_output_dir=tmp_path / "results",
    )
    process_runner = FakeProcessRunner()

    CodexExecRunner(config, process_runner).run(
        "private-server-buildout",
        project_path,
        prompt_path,
        "do the work",
        execution_preferences={
            "model": "gpt-5.6-terra",
            "reasoning_effort": "high",
        },
    )

    command = process_runner.calls[0][0]
    exec_index = command.index("exec")
    assert command[command.index("--model") + 1] == "gpt-5.6-terra"
    assert command[command.index("-c") + 1] == 'model_reasoning_effort="high"'
    assert command.index("--model") < exec_index
    assert command.index("-c") < exec_index


@pytest.mark.parametrize("setting,sandbox,enabled", [
    (True, "workspace-write", True), (False, "workspace-write", False),
    ("true", "workspace-write", False), (True, "read-only", False),
])
def test_agent_network_access_is_explicit_and_preserves_filesystem_sandbox(tmp_path, setting, sandbox, enabled):
    cli_path = tmp_path / "codex.cmd"
    cli_path.write_text("placeholder", encoding="utf-8")
    config = BridgeConfig(hub="https://hub.example", token="test", prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json", codex_cli_path=cli_path,
        codex_exec_output_dir=tmp_path / "results", codex_exec_sandbox=sandbox,
        backend_options={"agent_network_access": setting})
    process = FakeProcessRunner()
    CodexExecRunner(config, process).run("project-one", tmp_path, tmp_path / "prompt.md", "work")
    command = process.calls[0][0]
    override = "sandbox_workspace_write.network_access=true"
    assert (override in command) == enabled
    assert command[command.index("--sandbox")+1] == sandbox
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    if enabled:
        assert command[command.index(override)-1] == "-c"


def test_dedicated_top_level_backend_options_survive_config_roundtrip(tmp_path):
    source = {"hub": "https://hub.example", "token": "test",
              "prompt_dir": str(tmp_path / "prompts"), "state_path": str(tmp_path / "state.json"),
              "bindings": [], "backend_options": {"agent_network_access": True}}
    config = BridgeConfig.from_mapping(source)
    assert config.runtime_configs()[0].backend_options == {"agent_network_access": True}
    assert BridgeConfig.from_mapping(config.to_mapping()).backend_options == {"agent_network_access": True}


def test_configured_agent_model_is_explicit_even_when_user_config_is_ignored(tmp_path):
    cli_path = tmp_path / "codex.cmd"
    cli_path.write_text("placeholder", encoding="utf-8")
    config = BridgeConfig(hub="https://hub.example", token="private-device-token", prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json", codex_cli_path=cli_path, codex_exec_output_dir=tmp_path / "results",
        backend_options={"codex_model": "gpt-6-astra", "windows_sandbox": "elevated"})
    process = FakeProcessRunner()
    CodexExecRunner(config, process).run("project-one", tmp_path, tmp_path / "prompt.md", "work")
    command = process.calls[0][0]
    assert command[command.index("--model")+1] == "gpt-6-astra"
    assert "--ignore-user-config" in command
    assert 'windows.sandbox="elevated"' in command
    assert command[command.index("--sandbox")+1] == "workspace-write"
    CodexExecRunner(config, process).run("project-one", tmp_path, tmp_path / "prompt.md", "work",
                                      execution_preferences={"model": "explicit-entry-model"})
    command = process.calls[-1][0]
    assert command[command.index("--model")+1] == "explicit-entry-model"


def test_failure_summary_and_diagnostics_expose_specific_cause_without_credentials(tmp_path):
    cli_path = tmp_path / "codex.cmd"
    cli_path.write_text("placeholder", encoding="utf-8")
    config = BridgeConfig(hub="https://hub.example", token="private-device-token", prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json", codex_cli_path=cli_path, codex_exec_output_dir=tmp_path / "results")

    class FailedProcess:
        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", 'user\nprivate prompt text\n'
                'ERROR: model requires newer Codex; token=private-device-token; api_key="sk-abcdefghijklmnop"\n')

    result = CodexExecRunner(config, FailedProcess()).run("project-one", tmp_path, tmp_path / "prompt.md", "work")
    assert "requires newer Codex" in result.summary
    assert "private prompt text" not in result.summary
    assert "private-device-token" not in result.summary
    assert "sk-abcdefghijklmnop" not in result.summary
    diagnostic = result.output_path.with_suffix(".diagnostic.json")
    data = json.loads(diagnostic.read_text(encoding="utf-8"))
    assert "private-device-token" not in json.dumps(data)
    assert "sk-abcdefghijklmnop" not in json.dumps(data)
