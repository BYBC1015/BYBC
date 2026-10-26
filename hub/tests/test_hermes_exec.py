import subprocess

from codex_bridge.codex_exec import CodexExecResult
from codex_bridge.config import (
    BridgeConfig,
    default_inbox_url,
    hub_workspace_url,
    load_config,
    sample_config,
    save_config,
    update_binding_runtime_settings,
    update_hermes_tool_settings,
    update_hub_client_settings,
)
from codex_bridge.hermes_exec import HermesExecRunner
from codex_bridge.runner import BridgeRunner


class FakeProcessRunner:
    def __init__(self, returncode=0, stdout="Hermes reply", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls = []

    def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


class EmptyHubClient:
    def get_inbox(self):
        return {"device": {"label": "hermes-device-a"}, "tasks": [], "mentions": []}


class MentionHubClient:
    def __init__(self):
        self.entries = []
        self.handled_mentions = []

    def get_inbox(self):
        return {
            "device": {"label": "hermes-device-a"},
            "tasks": [],
            "mentions": [
                {
                    "id": 5,
                    "project_slug": "private-server-buildout",
                    "project_name": "Private Server Buildout",
                    "primary_device_label": "codex-device-a",
                    "source_label": "BYBC",
                    "body_markdown": "@hermes-device-a review this",
                }
            ],
        }

    def claim_mention(self, project_slug, entry_id):
        return self.get_inbox()["mentions"][0]

    def mark_mention_handled(self, project_slug, entry_id):
        self.handled_mentions.append((project_slug, entry_id))

    def post_project_entry(self, project_slug, body_markdown, *, reply_to_entry_id=None):
        self.entries.append((project_slug, body_markdown))


class FakeExecutor:
    def run(self, project_slug, project_path, prompt_path, prompt):
        return CodexExecResult(
            ok=True,
            command=["hermes", "--oneshot"],
            returncode=0,
            stdout="@codex-device-a suggestion",
            stderr="",
            last_message="@codex-device-a suggestion",
            output_path=prompt_path.with_suffix(".last.md"),
        )


def make_config(tmp_path, **overrides):
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={},
        ignore_existing_on_first_run=False,
        execution_backend="hermes",
        hermes_cli_path=tmp_path / "hermes.exe",
        hermes_timeout_seconds=90,
        hermes_output_dir=tmp_path / "hermes-results",
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def test_hermes_exec_runner_invokes_oneshot_in_project_dir(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("# Hub mention", encoding="utf-8")
    hermes_cli = tmp_path / "hermes.exe"
    hermes_cli.write_text("", encoding="utf-8")
    process_runner = FakeProcessRunner(stdout="Hermes suggests keeping Codex primary.")
    config = make_config(
        tmp_path,
        hermes_model="nous/hermes-test",
        hermes_provider="local",
        hermes_toolsets="",
    )

    result = HermesExecRunner(config, process_runner).run(
        "private-server-buildout",
        project_dir,
        prompt_path,
        "Review this Hub item.",
    )

    assert result.ok is True
    assert isinstance(result, CodexExecResult)
    command, kwargs = process_runner.calls[0]
    assert command[:5] == [
        str(hermes_cli),
        "--model",
        "nous/hermes-test",
        "--provider",
        "local",
    ]
    assert command[-2] == "--oneshot"
    hermes_prompt = command[-1]
    assert "Review this Hub item." in hermes_prompt
    assert "advisory assistant" in hermes_prompt
    assert kwargs["cwd"] == str(project_dir.resolve())
    assert kwargs["timeout"] == 90
    assert result.last_message == "Hermes suggests keeping Codex primary."
    assert result.output_path.read_text(encoding="utf-8") == result.last_message


def test_hermes_exec_runner_reports_missing_cli_path(tmp_path):
    config = make_config(tmp_path, hermes_cli_path=tmp_path / "missing.exe")

    result = HermesExecRunner(config, FakeProcessRunner()).run(
        "project",
        tmp_path,
        tmp_path / "prompt.md",
        "hello",
    )

    assert result.ok is False
    assert "Hermes CLI not found" in result.summary


def test_hermes_exec_runner_applies_message_overrides_before_device_default(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("# Hub mention", encoding="utf-8")
    hermes_cli = tmp_path / "hermes.exe"
    hermes_cli.write_text("", encoding="utf-8")
    process_runner = FakeProcessRunner()
    config = make_config(tmp_path, hermes_model="device/default")

    HermesExecRunner(config, process_runner).run(
        "private-server-buildout",
        project_dir,
        prompt_path,
        "Review this Hub item.",
        execution_preferences={
            "model": "message/selected",
            "reasoning_effort": "xhigh",
        },
    )

    command = process_runner.calls[0][0]
    assert command[command.index("--model") + 1] == "message/selected"
    assert command[command.index("--reasoning") + 1] == "xhigh"


def test_config_round_trips_hermes_settings(tmp_path):
    path = tmp_path / "config.json"
    config = make_config(
        tmp_path,
        hermes_model="nous/hermes",
        hermes_provider="openrouter",
        hermes_toolsets="",
    )

    save_config(config, path)
    loaded = load_config(path)

    assert loaded.execution_backend == "hermes"
    assert loaded.hermes_cli_path == tmp_path / "hermes.exe"
    assert loaded.hermes_model == "nous/hermes"
    assert loaded.hermes_provider == "openrouter"
    assert loaded.hermes_timeout_seconds == 90
    assert loaded.hermes_output_dir == tmp_path / "hermes-results"


def test_sample_config_keeps_codex_default_and_exposes_hermes_options():
    config = sample_config()

    assert config["execution_backend"] == "codex"
    assert "hermes_cli_path" in config
    assert config["hermes_timeout_seconds"] == 1800


def test_multi_ai_config_expands_to_isolated_runtime_bindings(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    config = BridgeConfig.from_mapping(
        {
            "hub": "https://hub.example/hub",
            "token": "",
            "projects": {"project-a": str(tmp_path / "project-a")},
            "bindings": [
                {
                    "id": "codex-main",
                    "name": "Codex",
                    "token": "codex-token",
                    "execution_backend": "codex",
                    "interval_seconds": 20,
                    "open_codex": True,
                },
                {
                    "id": "hermes-main",
                    "name": "Hermes",
                    "token": "hermes-token",
                    "execution_backend": "hermes",
                    "interval_seconds": 30,
                    "open_codex": False,
                    "auto_execute_codex": True,
                },
            ],
        }
    )

    codex, hermes = config.runtime_configs()

    assert (codex.binding_id, codex.execution_backend) == ("codex-main", "codex")
    assert (hermes.binding_id, hermes.execution_backend) == ("hermes-main", "hermes")
    assert codex.state_path != hermes.state_path
    assert codex.prompt_dir != hermes.prompt_dir
    assert hermes.interval_seconds == 30
    assert hermes.open_codex is False
    assert hermes.auto_execute_codex is True


def test_multi_ai_config_round_trip_preserves_bindings(tmp_path):
    path = tmp_path / "config.json"
    config = BridgeConfig.from_mapping(
        {
            "hub": "https://hub.example/hub",
            "bindings": [
                {
                    "id": "codex",
                    "name": "Codex",
                    "token": "codex-token",
                    "backend": "codex",
                },
                {
                    "id": "hermes",
                    "name": "Hermes",
                    "token": "hermes-token",
                    "backend": "hermes",
                    "backend_options": {"profile": "default"},
                },
            ],
        }
    )

    save_config(config, path)
    loaded = load_config(path)

    assert [binding.id for binding in loaded.bindings] == ["codex", "hermes"]
    assert [binding.execution_backend for binding in loaded.bindings] == [
        "codex",
        "hermes",
    ]
    assert loaded.bindings[1].backend_options == {"profile": "default"}


def test_ui_settings_update_hermes_binding_and_hub_retry_policy(tmp_path):
    config = BridgeConfig.from_mapping(
        {
            "hub": "https://old.example/hub",
            "bindings": [
                {
                    "id": "hermes",
                    "token": "hermes-token",
                    "execution_backend": "hermes",
                }
            ],
        }
    )

    update_binding_runtime_settings(
        config,
        "hermes",
        enabled=True,
        interval_seconds=45,
        open_codex=False,
        auto_execute_codex=True,
    )
    update_hermes_tool_settings(
        config,
        hermes_cli_path=tmp_path / "hermes.exe",
        hermes_model="hermes/model",
        hermes_provider="local",
        hermes_toolsets="browser,terminal",
        hermes_timeout_seconds=600,
    )
    update_hub_client_settings(
        config,
        hub="https://new.example/hub/",
        timeout_seconds=9,
        get_retry_attempts=3,
        retry_backoff_seconds=0.5,
        inbox_url="https://new.example/inbox/messages",
    )

    binding = config.bindings[0]
    assert binding.interval_seconds == 45
    assert binding.auto_execute_codex is True
    assert binding.hermes_model == "hermes/model"
    assert binding.hermes_provider == "local"
    assert binding.hermes_toolsets == "browser,terminal"
    assert config.hermes_cli_path == tmp_path / "hermes.exe"
    assert config.hub == "https://new.example/hub"
    assert config.hub_request_timeout_seconds == 9
    assert config.hub_get_retry_attempts == 3
    assert config.hub_retry_backoff_seconds == 0.5
    assert config.inbox_url == "https://new.example/inbox/messages"


def test_public_app_urls_skip_internal_port_redirects():
    hub = "https://203.0.113.10/hub"

    assert hub_workspace_url(hub) == "https://203.0.113.10/hub/projects"
    assert default_inbox_url(hub) == "https://203.0.113.10/inbox/messages"


def test_bridge_runner_selects_hermes_executor_from_config(tmp_path):
    config = make_config(tmp_path)

    runner = BridgeRunner(config, client=EmptyHubClient())

    assert isinstance(runner.executor, HermesExecRunner)


def test_hermes_backend_uses_hermes_title_when_posting_mention_result(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    config = make_config(
        tmp_path,
        projects={"private-server-buildout": project_dir},
        auto_execute_codex=True,
    )
    client = MentionHubClient()

    BridgeRunner(config, client=client, executor=FakeExecutor()).process_once()

    assert "HermesBridge 自动执行提及 #5" in client.entries[0][1]
    assert "@codex-device-a" in client.entries[0][1]
