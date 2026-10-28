"""CLI path recovery uses local fixtures and never starts a real model."""
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from codex_bridge import model_catalog, tool_locator
from codex_bridge.agent_runtime import AgentDecisionError, CodexAgentDecisionRunner
from codex_bridge.codex_exec import CodexExecRunner
from codex_bridge.model_allocator import decide_model


@pytest.fixture
def installations(tmp_path, monkeypatch):
    roaming = tmp_path / "用户 安装" / "Roaming"
    local = tmp_path / "用户 安装" / "Local"
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(model_catalog.shutil, "which", lambda *_: None)
    return SimpleNamespace(
        npm=roaming / "npm" / "codex.cmd",
        desktop=local / "OpenAI" / "Codex" / "bin",
    )


def installed(path, timestamp=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("test fixture, never execute", encoding="utf-8")
    if timestamp is not None:
        os.utime(path, (timestamp, timestamp))
    return path


def test_existing_explicit_cli_wins_even_over_newer_installation(installations):
    old = installed(installations.desktop / "old-hash" / "codex.exe", 100)
    installed(installations.desktop / "new-hash" / "codex.exe", 200)
    installed(installations.npm)
    assert tool_locator.resolve_codex_cli_path(old) == old


def test_missing_desktop_hash_prefers_latest_same_family_over_npm(installations):
    installed(installations.npm)
    installed(installations.desktop / "z-older-hash" / "codex.exe", 100)
    latest = installed(installations.desktop / "a-newer-hash" / "codex.exe", 200)
    assert tool_locator.resolve_codex_cli_path(installations.desktop / "removed" / "codex.exe") == latest


def test_missing_cli_can_fall_back_to_known_npm_installation(tmp_path, installations):
    npm = installed(installations.npm)
    assert tool_locator.resolve_codex_cli_path(tmp_path / "missing.exe") == npm


def test_missing_desktop_family_never_switches_to_unrelated_npm(installations):
    installed(installations.npm)
    assert tool_locator.resolve_codex_cli_path(installations.desktop / "removed" / "codex.exe") is None


def test_default_discovery_keeps_prior_order_and_does_not_select_desktop_gui(tmp_path, installations):
    installed(installations.desktop / "current-hash" / "codex.exe")
    installed(installations.desktop.parent / "app" / "Codex.exe")
    assert tool_locator.find_default_codex_cli_path() is None
    npm = installed(installations.npm)
    installed(npm.with_suffix(""))
    assert tool_locator.find_default_codex_cli_path() == npm
    directory = tmp_path / "not-a-cli.exe"
    directory.mkdir()
    assert tool_locator.find_default_codex_cli_path([directory, npm]) == npm


def test_missing_candidates_return_none(installations):
    installations.npm.mkdir(parents=True)
    assert tool_locator.resolve_codex_cli_path(installations.desktop / "removed" / "codex.exe") is None


MODELS = [{"model": "gpt-6-sol", "supportedReasoningEfforts": [{"reasoningEffort": "medium"}]}]
CHOICE = {"model": "gpt-6-sol", "reasoning_effort": "medium", "reason": "Fixture task"}
DECISION = {"action": "new", "conversation_id": None, "title": "Fixture", "reason": "New work"}


class RecordingProcess:
    def __init__(self, payload="done", returncode=0):
        self.payload, self.returncode, self.calls = payload, returncode, []

    def run(self, command, **options):
        self.calls.append((command, options))
        Path(command[command.index("--output-last-message") + 1]).write_text(
            self.payload if isinstance(self.payload, str) else json.dumps(self.payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, self.returncode, "", "")


def config_for_cli(tmp_path, path):
    return SimpleNamespace(codex_cli_path=path, codex_exec_output_dir=tmp_path / "outputs", backend_options={})


@pytest.mark.parametrize("consumer", ["execution", "conversation", "allocation", "catalog"])
def test_all_cli_consumers_recover_desktop_upgrade_without_changing_scope(tmp_path, installations, monkeypatch, consumer):
    installed(installations.npm)
    selected = installed(installations.desktop / "new-hash" / "codex.exe")
    missing = installations.desktop / "old-hash" / "codex.exe"
    config = config_for_cli(tmp_path, missing)
    if consumer == "catalog":
        calls = []
        monkeypatch.setattr(model_catalog, "_read_app_server_catalog", lambda path, timeout:
                            calls.append(path) or {"models": MODELS})
        assert model_catalog.discover_codex_catalog(missing, tmp_path / "cache")["models"] == MODELS
        assert calls == [selected]
        return
    process = RecordingProcess(DECISION if consumer == "conversation" else CHOICE)
    if consumer == "execution":
        assert CodexExecRunner(config, process).run("project", tmp_path, tmp_path / "prompt.md", "work").ok
    elif consumer == "conversation":
        assert CodexAgentDecisionRunner(config, process).decide({"conversations": []}) == DECISION
    else:
        assert decide_model(config, {"models": MODELS}, process) == CHOICE
    command = process.calls[0][0]
    assert command[0] == str(selected)
    assert config.codex_cli_path == missing
    assert "--ignore-user-config" in command
    assert command[command.index("--sandbox") + 1] == ("workspace-write" if consumer == "execution" else "read-only")
    if consumer != "execution":
        assert "--ephemeral" in command
        assert command[command.index("--cd") + 1] != str(tmp_path)


@pytest.mark.parametrize("consumer", ["execution", "conversation", "allocation", "catalog"])
def test_missing_desktop_cli_does_not_launch_unrelated_installation(tmp_path, installations, monkeypatch, consumer):
    installed(installations.npm)
    missing = installations.desktop / "removed" / "codex.exe"
    config = config_for_cli(tmp_path, missing)
    process = RecordingProcess()
    if consumer == "execution":
        result = CodexExecRunner(config, process).run("project", tmp_path, tmp_path / "prompt.md", "work")
        assert not result.ok and "not found" in result.error
    elif consumer == "conversation":
        with pytest.raises(AgentDecisionError):
            CodexAgentDecisionRunner(config, process).decide({})
    elif consumer == "allocation":
        with pytest.raises(AgentDecisionError):
            decide_model(config, {"models": MODELS}, process)
    else:
        monkeypatch.setattr(model_catalog, "_read_app_server_catalog", lambda *_: pytest.fail("Unexpected CLI launch"))
        assert model_catalog.discover_codex_catalog(missing, tmp_path / "cache")["models"] == []
    assert process.calls == []


def test_existing_cli_failure_is_not_retried_with_another_installation(tmp_path, installations):
    selected = installed(installations.desktop / "old-hash" / "codex.exe")
    installed(installations.npm)
    config = config_for_cli(tmp_path, selected)
    process = RecordingProcess(returncode=1)
    result = CodexExecRunner(config, process).run("project", tmp_path, tmp_path / "prompt.md", "work")
    assert not result.ok
    assert len(process.calls) == 1 and process.calls[0][0][0] == str(selected)


@pytest.mark.parametrize("ignore_config", [True, False])
def test_execution_user_config_switch_preserves_requested_policy(tmp_path, installations, ignore_config):
    cli = installed(installations.npm)
    config = config_for_cli(tmp_path, cli)
    config.codex_exec_ignore_user_config = ignore_config
    process = RecordingProcess()
    assert CodexExecRunner(config, process).run("project", tmp_path, tmp_path / "prompt.md", "work").ok
    command = process.calls[0][0]
    assert ("--ignore-user-config" in command) is ignore_config
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert command[command.index("--ask-for-approval") + 1] == "never"
