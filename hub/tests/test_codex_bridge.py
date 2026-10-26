from pathlib import Path
import pytest
from urllib.parse import parse_qs, urlparse

from codex_bridge import cli
from codex_bridge.client import HubClientError
from codex_bridge.codex_exec import CodexExecResult
from codex_bridge.config import (
    BridgeConfig,
    load_config,
    sample_config,
    save_config,
    update_local_tool_settings,
)
from codex_bridge.runner import (
    BridgeRunner,
    apply_device_mention_policy,
    execution_preferences_for_entry,
)


class FakeHubClient:
    def __init__(self, inbox):
        self.inbox = inbox
        self.task_prompt = inbox.get("task_prompt")
        self.project_memory = inbox.get("project_memory", "")
        self.project_runtime_memory = inbox.get(
            "project_runtime_memory", self.project_memory
        )
        self.claimed_tasks = []
        self.claimed_mentions = []
        self.handled_mentions = []
        self.dispatching_mentions = []
        self.submitted_mentions = []
        self.released_mentions = []
        self.entries = []
        self.entry_reply_ids = []
        self.task_entries = []
        self.task_entry_reply_ids = []
        self.task_status_updates = []
        self.prompt_requests = []
        self.memory_requests = []
        self.runtime_memory_requests = []
        self.sync_snapshots = []

    def get_inbox(self):
        return self.inbox

    def claim_task(self, project_slug, task_id):
        self.claimed_tasks.append((project_slug, task_id))
        task = dict(self.inbox["tasks"][0])
        task["status"] = "claimed"
        task["claimed_device_label"] = self.inbox["device"]["label"]
        return task

    def get_task_prompt(self, project_slug, task_id):
        self.prompt_requests.append((project_slug, task_id))
        return self.task_prompt or f"# Codex Task\n\nTask {task_id} in {project_slug}"

    def get_project_memory_markdown(self, project_slug):
        self.memory_requests.append(project_slug)
        return self.project_memory

    def get_project_memory_runtime_markdown(self, project_slug):
        self.runtime_memory_requests.append(project_slug)
        return self.project_runtime_memory

    def post_project_sync_snapshot(
        self, project_slug, *, memory_version, git_commit, manifest
    ):
        self.sync_snapshots.append(
            (project_slug, memory_version, git_commit, manifest)
        )
        return {"snapshot": {"revision": len(self.sync_snapshots)}}

    def claim_mention(self, project_slug, entry_id):
        self.claimed_mentions.append((project_slug, entry_id))
        return dict(self.inbox["mentions"][0])

    def mark_mention_handled(self, project_slug, entry_id):
        self.handled_mentions.append((project_slug, entry_id))

    def mark_mention_dispatching(self, project_slug, entry_id):
        self.dispatching_mentions.append((project_slug, entry_id))
        return {"status": "dispatching", "delivery_id": f"delivery-{entry_id}"}

    def mark_mention_submitted(self, project_slug, entry_id):
        self.submitted_mentions.append((project_slug, entry_id))
        return {"status": "submitted", "delivery_id": f"delivery-{entry_id}"}

    def release_mention_dispatch(self, project_slug, entry_id):
        self.released_mentions.append((project_slug, entry_id))
        return {"status": "claimed", "delivery_id": f"delivery-{entry_id}"}

    def update_task_status(self, project_slug, task_id, status, result_summary=""):
        self.task_status_updates.append(
            (project_slug, task_id, status, result_summary)
        )

    def post_task_entry(self, project_slug, task_id, body_markdown, *, reply_to_entry_id=None):
        self.task_entries.append((project_slug, task_id, body_markdown))
        self.task_entry_reply_ids.append(reply_to_entry_id)

    def post_project_entry(self, project_slug, body_markdown, *, reply_to_entry_id=None):
        self.entries.append((project_slug, body_markdown))
        self.entry_reply_ids.append(reply_to_entry_id)


class FakeLauncher:
    def __init__(self):
        self.urls = []

    def open(self, url):
        self.urls.append(url)


class FakeSubmitter:
    def __init__(self):
        self.calls = []

    def submit_after_delay(
        self,
        delay_seconds,
        hotkey,
        expected_prompt="",
        reopen_callback=None,
    ):
        self.calls.append((delay_seconds, hotkey, expected_prompt))
        return True


class SuccessfulSubmitter(FakeSubmitter):
    def submit_after_delay(
        self,
        delay_seconds,
        hotkey,
        expected_prompt="",
        reopen_callback=None,
    ):
        super().submit_after_delay(
            delay_seconds,
            hotkey,
            expected_prompt,
            reopen_callback,
        )
        return True


class DeliveryRecoveredSubmitter(SuccessfulSubmitter):
    def __init__(self):
        super().__init__()
        self.last_recovery_action = "start_codex"


class FailedSubmitter(FakeSubmitter):
    last_error = "未确认草稿已提交"

    def submit_after_delay(
        self,
        delay_seconds,
        hotkey,
        expected_prompt="",
        reopen_callback=None,
    ):
        super().submit_after_delay(
            delay_seconds,
            hotkey,
            expected_prompt,
            reopen_callback,
        )
        return False


class AmbiguousSubmitter(FailedSubmitter):
    def __init__(self):
        super().__init__()
        self.last_send_attempted = False

    def submit_after_delay(
        self,
        delay_seconds,
        hotkey,
        expected_prompt="",
        reopen_callback=None,
    ):
        super().submit_after_delay(
            delay_seconds,
            hotkey,
            expected_prompt,
            reopen_callback,
        )
        self.last_send_attempted = True
        self.last_error = "Codex 编辑器暂时不可见，尚未确认提交。"
        return False


class FailHandledOnceClient(FakeHubClient):
    def __init__(self, inbox):
        super().__init__(inbox)
        self.fail_handled_once = True

    def mark_mention_handled(self, project_slug, entry_id):
        self.handled_mentions.append((project_slug, entry_id))
        if self.fail_handled_once:
            self.fail_handled_once = False
            raise HubClientError("handled response timed out")


class FakeExecutor:
    def __init__(self, ok=True, summary="Codex finished."):
        self.ok = ok
        self.summary = summary
        self.calls = []

    def run(
        self,
        project_slug,
        project_path,
        prompt_path,
        prompt,
        execution_preferences=None,
    ):
        self.calls.append(
            (
                project_slug,
                project_path,
                prompt_path,
                prompt,
                execution_preferences,
            )
        )
        return CodexExecResult(
            ok=self.ok,
            command=["codex", "exec"],
            returncode=0 if self.ok else 1,
            stdout=self.summary,
            stderr="" if self.ok else self.summary,
            last_message=self.summary if self.ok else "",
            output_path=prompt_path.with_suffix(".last.md"),
            error="" if self.ok else self.summary,
        )


class UnclaimableTaskClient(FakeHubClient):
    def claim_task(self, project_slug, task_id):
        self.claimed_tasks.append((project_slug, task_id))
        return None


class UnclaimableMentionClient(FakeHubClient):
    def claim_mention(self, project_slug, entry_id):
        self.claimed_mentions.append((project_slug, entry_id))
        return None


class MultiMentionClient(FakeHubClient):
    def claim_mention(self, project_slug, entry_id):
        self.claimed_mentions.append((project_slug, entry_id))
        return dict(
            next(item for item in self.inbox["mentions"] if item["id"] == entry_id)
        )


def prompt_from_codex_url(url):
    return parse_qs(urlparse(url).query)["prompt"][0]


def test_device_mention_policy_primary_device_does_not_mention_itself():
    body = "Codex 已完成检查。"

    assert apply_device_mention_policy(
        body,
        primary_device_label="codex-device-a",
        current_device_label="codex-device-a",
    ) == body


def test_device_mention_policy_secondary_device_notifies_primary_once():
    body = "Hermes 已完成建议。"

    result = apply_device_mention_policy(
        body,
        primary_device_label="codex-device-a",
        current_device_label="hermes-device-a",
    )

    assert result == "@codex-device-a\n\nHermes 已完成建议。"
    assert apply_device_mention_policy(
        result,
        primary_device_label="codex-device-a",
        current_device_label="hermes-device-a",
    ) == result


def test_device_mention_policy_requires_an_exact_existing_mention():
    body = "@codex-device-ab 不是主设备。"

    result = apply_device_mention_policy(
        body,
        primary_device_label="codex-device-a",
        current_device_label="hermes-device-a",
    )

    assert result.startswith("@codex-device-a\n\n")


def test_device_mention_policy_without_primary_leaves_reply_unchanged():
    body = "普通回复，不唤醒其他设备。"

    assert apply_device_mention_policy(
        body,
        primary_device_label="",
        current_device_label="hermes-device-a",
    ) == body


def test_collaboration_delivery_prompt_assigns_advisor_role(tmp_path):
    config = BridgeConfig(
        hub="https://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
    )
    runner = BridgeRunner(
        config,
        client=FakeHubClient({"device": {}, "tasks": [], "mentions": []}),
        launcher=FakeLauncher(),
    )

    prompt = runner._build_mention_prompt(
        {
            "id": 12,
            "project_slug": "shared-work",
            "project_name": "Shared Work",
            "source_label": "owner",
            "body_markdown": "Review the plan.",
            "primary_device_label": "codex-device-a",
            "delivery_reason": "collaboration",
            "collaboration_role": "advisor",
            "group_chat_roles": [
                {
                    "name": "security-review",
                    "instructions": "Check permissions and trust boundaries.",
                }
            ],
        },
        "hermes-device-a",
    )

    assert "# Codex Hub Collaboration" in prompt
    assert "advisory collaborator" in prompt
    assert "find omissions, risks, and weak assumptions" in prompt
    assert "@codex-device-a" in prompt
    assert "final decision maker" in prompt
    assert "Assigned Group Chat Roles" in prompt
    assert "@security-review" in prompt
    assert "trust boundaries" in prompt


def test_runner_claims_task_writes_prompt_and_opens_codex(tmp_path):
    client = FakeHubClient(
        {
            "device": {"label": "codex-device-a"},
            "tasks": [
                {
                    "id": 42,
                    "project_slug": "private-server-buildout",
                    "project_name": "Private Server Buildout",
                    "title": "Check bridge",
                    "priority": "low",
                    "status": "open",
                }
            ],
            "mentions": [],
        }
    )
    launcher = FakeLauncher()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={"private-server-buildout": project_dir},
        ignore_existing_on_first_run=False,
    )

    result = BridgeRunner(config, client=client, launcher=launcher).process_once()

    assert result.claimed_tasks == [42]
    assert client.claimed_tasks == [("private-server-buildout", 42)]
    assert client.prompt_requests == [("private-server-buildout", 42)]
    prompt_files = list((tmp_path / "prompts").glob("*.md"))
    assert len(prompt_files) == 1
    assert "# Codex Task" in prompt_files[0].read_text(encoding="utf-8")
    assert launcher.urls
    assert launcher.urls[0].startswith("codex://new?")
    assert "path=" in launcher.urls[0]
    assert "prompt=" in launcher.urls[0]
    launch_prompt = prompt_from_codex_url(launcher.urls[0])
    assert "# Codex Task" in launch_prompt
    assert "Task 42 in private-server-buildout" in launch_prompt


def test_runner_auto_submits_visible_codex_thread_when_enabled(tmp_path):
    client = FakeHubClient(
        {
            "device": {"label": "codex-device-a"},
            "tasks": [],
            "mentions": [
                {
                    "id": 8,
                    "project_slug": "private-server-buildout",
                    "project_name": "Private Server Buildout",
                    "source_label": "BYBC",
                    "body_markdown": "@codex-device-a please check visible app mode",
                }
            ],
        }
    )
    launcher = FakeLauncher()
    submitter = FakeSubmitter()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={"private-server-buildout": project_dir},
        ignore_existing_on_first_run=False,
        auto_execute_codex=False,
        auto_submit_codex_prompt=True,
        codex_submit_delay_seconds=0.25,
        codex_submit_hotkey="enter",
    )

    result = BridgeRunner(
        config,
        client=client,
        launcher=launcher,
        submitter=submitter,
    ).process_once()

    assert result.claimed_mentions == [8]
    assert result.executed_codex == []
    assert launcher.urls
    assert len(submitter.calls) == 1
    delay, hotkey, expected_prompt = submitter.calls[0]
    assert (delay, hotkey) == (0.25, "enter")
    assert "@codex-device-a please check visible app mode" in expected_prompt


def test_visible_mode_dispatches_only_one_mention_per_poll(tmp_path):
    mentions = [
        {
            "id": entry_id,
            "project_slug": "private-server-buildout",
            "project_name": "Private Server Buildout",
            "source_label": "BYBC",
            "body_markdown": f"@codex-device-a visible item {entry_id}",
        }
        for entry_id in (71, 72)
    ]
    client = MultiMentionClient(
        {
            "device": {"label": "codex-device-a"},
            "tasks": [],
            "mentions": mentions,
        }
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={"private-server-buildout": project_dir},
        ignore_existing_on_first_run=False,
        auto_execute_codex=False,
        auto_submit_codex_prompt=True,
        codex_submit_delay_seconds=0,
    )
    submitter = SuccessfulSubmitter()
    runner = BridgeRunner(
        config,
        client=client,
        launcher=FakeLauncher(),
        submitter=submitter,
    )

    first = runner.process_once()
    second = runner.process_once()

    assert first.claimed_mentions == [71]
    assert second.claimed_mentions == [72]
    assert len(submitter.calls) == 2


def test_runner_reports_successful_visible_codex_submission(tmp_path):
    client = FakeHubClient(
        {
            "device": {"label": "codex-device-a"},
            "tasks": [],
            "mentions": [
                {
                    "id": 18,
                    "project_slug": "private-server-buildout",
                    "project_name": "Private Server Buildout",
                    "source_label": "BYBC",
                    "body_markdown": "@codex-device-a submit this prompt",
                }
            ],
        }
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={"private-server-buildout": project_dir},
        ignore_existing_on_first_run=False,
        auto_submit_codex_prompt=True,
        codex_submit_delay_seconds=0.25,
    )

    result = BridgeRunner(
        config,
        client=client,
        launcher=FakeLauncher(),
        submitter=SuccessfulSubmitter(),
    ).process_once()

    assert "Codex 提示已自动发送。" in result.messages


def test_runner_reports_event_driven_codex_start_for_failed_delivery(tmp_path):
    client = FakeHubClient(
        {
            "device": {"label": "codex-device-a"},
            "tasks": [],
            "mentions": [
                {
                    "id": 180,
                    "project_slug": "private-server-buildout",
                    "project_name": "Private Server Buildout",
                    "source_label": "BYBC",
                    "body_markdown": "@codex-device-a recover only for this delivery",
                }
            ],
        }
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={"private-server-buildout": project_dir},
        ignore_existing_on_first_run=False,
        auto_submit_codex_prompt=True,
        codex_submit_delay_seconds=0,
    )

    result = BridgeRunner(
        config,
        client=client,
        launcher=FakeLauncher(),
        submitter=DeliveryRecoveredSubmitter(),
    ).process_once()

    assert (
        "Hub 指令首次未送达；检测到 Codex 未运行，已按需启动并重试。"
        in result.messages
    )
    assert "Codex 提示已自动发送。" in result.messages


def test_runner_locks_mention_when_visible_submission_fails_without_send(tmp_path):
    client = FakeHubClient(
        {
            "device": {"label": "codex-device-a"},
            "tasks": [],
            "mentions": [
                {
                    "id": 19,
                    "project_slug": "private-server-buildout",
                    "project_name": "Private Server Buildout",
                    "source_label": "BYBC",
                    "body_markdown": "@codex-device-a retry this prompt",
                }
            ],
        }
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={"private-server-buildout": project_dir},
        ignore_existing_on_first_run=False,
        auto_submit_codex_prompt=True,
        codex_submit_delay_seconds=0,
    )
    first_launcher = FakeLauncher()
    runner = BridgeRunner(
        config,
        client=client,
        launcher=first_launcher,
        submitter=FailedSubmitter(),
    )

    first = runner.process_once()
    second = runner.process_once()
    restarted_launcher = FakeLauncher()
    restarted = BridgeRunner(
        config,
        client=client,
        launcher=restarted_launcher,
        submitter=FailedSubmitter(),
    ).process_once()

    assert client.handled_mentions == []
    assert client.entries == []
    assert "19" not in runner.state.seen_mention_ids
    assert len(first_launcher.urls) == 1
    assert restarted_launcher.urls == []
    assert client.claimed_mentions == [("private-server-buildout", 19)]
    assert client.dispatching_mentions == [("private-server-buildout", 19)]
    assert client.released_mentions == []
    assert first.pending_mentions == [19]
    assert second.pending_mentions == [19]
    assert restarted.pending_mentions == [19]
    saved = BridgeRunner(
        config,
        client=client,
        launcher=FakeLauncher(),
        submitter=FailedSubmitter(),
    ).state.get_mention_delivery("private-server-buildout", 19)
    assert saved is not None
    assert saved["stage"] == "uncertain"
    assert saved["detail"] == FailedSubmitter.last_error
    assert any("未执行发送动作；已锁定为待确认" in line for line in first.messages)
    assert any("不会自动重新打开 Codex" in line for line in first.messages)


def test_runner_locks_ambiguous_submission_across_polls_and_restart(tmp_path):
    inbox = {
        "device": {"label": "codex-device-a"},
        "tasks": [],
        "mentions": [
            {
                "id": 64,
                "project_slug": "private-server-buildout",
                "project_name": "Private Server Buildout",
                "source_label": "BYBC",
                "body_markdown": "@codex-device-a query quota",
            }
        ],
    }
    client = FakeHubClient(inbox)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={"private-server-buildout": project_dir},
        ignore_existing_on_first_run=False,
        auto_submit_codex_prompt=True,
        codex_submit_delay_seconds=0,
    )
    first_launcher = FakeLauncher()
    runner = BridgeRunner(
        config,
        client=client,
        launcher=first_launcher,
        submitter=AmbiguousSubmitter(),
    )

    first = runner.process_once()
    second = runner.process_once()
    restarted_launcher = FakeLauncher()
    restarted = BridgeRunner(
        config,
        client=client,
        launcher=restarted_launcher,
        submitter=AmbiguousSubmitter(),
    ).process_once()

    assert len(first_launcher.urls) == 1
    assert restarted_launcher.urls == []
    assert client.claimed_mentions == [("private-server-buildout", 64)]
    assert client.handled_mentions == []
    assert first.pending_mentions == [64]
    assert second.pending_mentions == [64]
    assert restarted.pending_mentions == [64]
    saved = BridgeRunner(
        config,
        client=client,
        launcher=FakeLauncher(),
        submitter=AmbiguousSubmitter(),
    ).state.get_mention_delivery("private-server-buildout", 64)
    assert saved is not None
    assert saved["stage"] == "uncertain"
    assert any("不会自动重发" in line for line in first.messages)


def test_runner_retries_only_handled_receipt_after_successful_send(tmp_path):
    inbox = {
        "device": {"label": "codex-device-a"},
        "tasks": [],
        "mentions": [
            {
                "id": 65,
                "project_slug": "private-server-buildout",
                "project_name": "Private Server Buildout",
                "source_label": "BYBC",
                "body_markdown": "@codex-device-a do once",
            }
        ],
    }
    client = FailHandledOnceClient(inbox)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={"private-server-buildout": project_dir},
        ignore_existing_on_first_run=False,
        auto_submit_codex_prompt=True,
        codex_submit_delay_seconds=0,
    )
    first_launcher = FakeLauncher()
    first = BridgeRunner(
        config,
        client=client,
        launcher=first_launcher,
        submitter=SuccessfulSubmitter(),
    ).process_once()
    second_launcher = FakeLauncher()
    second_runner = BridgeRunner(
        config,
        client=client,
        launcher=second_launcher,
        submitter=SuccessfulSubmitter(),
    )
    second = second_runner.process_once()

    assert len(first_launcher.urls) == 1
    assert second_launcher.urls == []
    assert client.claimed_mentions == [("private-server-buildout", 65)]
    assert client.handled_mentions == [
        ("private-server-buildout", 65),
        ("private-server-buildout", 65),
    ]
    assert first.pending_mentions == [65]
    assert second.pending_mentions == []
    assert "65" in second_runner.state.seen_mention_ids
    assert second_runner.state.get_mention_delivery("private-server-buildout", 65) is None


def test_runner_locks_task_when_visible_submission_fails_without_send(tmp_path):
    client = FakeHubClient(
        {
            "device": {"label": "codex-device-a"},
            "tasks": [
                {
                    "id": 44,
                    "project_slug": "private-server-buildout",
                    "project_name": "Private Server Buildout",
                    "title": "Retry visible task",
                    "priority": "normal",
                    "status": "open",
                }
            ],
            "mentions": [],
        }
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={"private-server-buildout": project_dir},
        ignore_existing_on_first_run=False,
        auto_submit_codex_prompt=True,
        codex_submit_delay_seconds=0,
    )
    first_launcher = FakeLauncher()
    runner = BridgeRunner(
        config,
        client=client,
        launcher=first_launcher,
        submitter=FailedSubmitter(),
    )

    first = runner.process_once()
    second = runner.process_once()
    restarted_launcher = FakeLauncher()
    restarted = BridgeRunner(
        config,
        client=client,
        launcher=restarted_launcher,
        submitter=FailedSubmitter(),
    ).process_once()

    assert "44" not in runner.state.seen_task_ids
    assert len(first_launcher.urls) == 1
    assert restarted_launcher.urls == []
    assert client.claimed_tasks == [("private-server-buildout", 44)]
    assert first.pending_tasks == [44]
    assert second.pending_tasks == [44]
    assert restarted.pending_tasks == [44]
    saved = BridgeRunner(
        config,
        client=client,
        launcher=FakeLauncher(),
        submitter=FailedSubmitter(),
    ).state.get_task_delivery("private-server-buildout", 44)
    assert saved is not None
    assert saved["stage"] == "uncertain"
    assert saved["detail"] == FailedSubmitter.last_error
    assert any("未执行发送动作；已锁定为待确认" in line for line in first.messages)
    assert any("不会自动重新打开 Codex" in line for line in first.messages)


def test_runner_locks_ambiguous_task_across_polls_and_restart(tmp_path):
    inbox = {
        "device": {"label": "codex-device-a"},
        "tasks": [
            {
                "id": 45,
                "project_slug": "private-server-buildout",
                "project_name": "Private Server Buildout",
                "title": "Send exactly once",
                "priority": "normal",
                "status": "open",
            }
        ],
        "mentions": [],
    }
    client = FakeHubClient(inbox)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={"private-server-buildout": project_dir},
        ignore_existing_on_first_run=False,
        auto_submit_codex_prompt=True,
        codex_submit_delay_seconds=0,
    )
    first_launcher = FakeLauncher()
    runner = BridgeRunner(
        config,
        client=client,
        launcher=first_launcher,
        submitter=AmbiguousSubmitter(),
    )

    first = runner.process_once()
    second = runner.process_once()
    restarted_launcher = FakeLauncher()
    restarted = BridgeRunner(
        config,
        client=client,
        launcher=restarted_launcher,
        submitter=AmbiguousSubmitter(),
    ).process_once()

    assert len(first_launcher.urls) == 1
    assert restarted_launcher.urls == []
    assert client.claimed_tasks == [("private-server-buildout", 45)]
    assert first.pending_tasks == [45]
    assert second.pending_tasks == [45]
    assert restarted.pending_tasks == [45]
    saved = BridgeRunner(
        config,
        client=client,
        launcher=FakeLauncher(),
        submitter=AmbiguousSubmitter(),
    ).state.get_task_delivery("private-server-buildout", 45)
    assert saved is not None
    assert saved["stage"] == "uncertain"
    assert any("不会自动重发" in line for line in first.messages)


def test_runner_reports_unclaimable_task_in_chinese(tmp_path):
    client = UnclaimableTaskClient(
        {
            "device": {"label": "codex-device-a"},
            "tasks": [
                {
                    "id": 42,
                    "project_slug": "private-server-buildout",
                    "project_name": "Private Server Buildout",
                    "title": "Check bridge",
                    "priority": "low",
                    "status": "open",
                }
            ],
            "mentions": [],
        }
    )
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={},
        ignore_existing_on_first_run=False,
    )

    result = BridgeRunner(config, client=client, launcher=FakeLauncher()).process_once()

    assert "任务 #42 无法领取。" in result.messages


def test_runner_claims_handles_and_acks_mention_once(tmp_path):
    client = FakeHubClient(
        {
            "device": {"label": "codex-device-a"},
            "tasks": [],
            "mentions": [
                {
                    "id": 7,
                    "project_slug": "private-server-buildout",
                    "project_name": "Private Server Buildout",
                    "source_label": "BYBC",
                    "body_markdown": "@codex-device-a please check this",
                }
            ],
        }
    )
    launcher = FakeLauncher()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={"private-server-buildout": project_dir},
        ignore_existing_on_first_run=False,
    )
    runner = BridgeRunner(config, client=client, launcher=launcher)

    first = runner.process_once()
    second = runner.process_once()

    assert first.claimed_mentions == [7]
    assert second.claimed_mentions == []
    assert client.claimed_mentions == [("private-server-buildout", 7)]
    assert client.handled_mentions == [("private-server-buildout", 7)]
    assert len(client.entries) == 1
    assert "已收到提及 #7" in client.entries[0][1]
    assert launcher.urls
    launch_prompt = prompt_from_codex_url(launcher.urls[0])
    assert "@codex-device-a please check this" in launch_prompt
    assert "Prompt file:" in launch_prompt


def test_runner_retries_mention_when_claim_fails(tmp_path):
    client = UnclaimableMentionClient(
        {
            "device": {"label": "hermes-device-a"},
            "tasks": [],
            "mentions": [
                {
                    "id": 42,
                    "project_slug": "private-server-buildout",
                    "project_name": "Private Server Buildout",
                    "source_label": "BYBC",
                    "body_markdown": "@hermes-device-a hello",
                }
            ],
        }
    )
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={},
        ignore_existing_on_first_run=False,
    )
    runner = BridgeRunner(config, client=client, launcher=FakeLauncher())

    first = runner.process_once()
    second = runner.process_once()

    assert first.claimed_mentions == []
    assert second.claimed_mentions == []
    assert client.claimed_mentions == [
        ("private-server-buildout", 42),
        ("private-server-buildout", 42),
    ]
    assert "42" not in runner.state.seen_mention_ids
    assert client.handled_mentions == []
    assert client.entries == []


def test_runner_assistant_mention_reply_mentions_primary_device(tmp_path):
    client = FakeHubClient(
        {
            "device": {"label": "hermes-device-a"},
            "tasks": [],
            "mentions": [
                {
                    "id": 9,
                    "project_slug": "private-server-buildout",
                    "project_name": "Private Server Buildout",
                    "primary_device_label": "codex-device-a",
                    "source_label": "BYBC",
                    "body_markdown": "@hermes-device-a please review this",
                }
            ],
        }
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={"private-server-buildout": project_dir},
        ignore_existing_on_first_run=False,
    )

    BridgeRunner(config, client=client, launcher=FakeLauncher()).process_once()

    assert client.entries[0][1].startswith("@codex-device-a")
    prompt_text = next((tmp_path / "prompts").glob("*.md")).read_text(encoding="utf-8")
    assert "Primary device: @codex-device-a" in prompt_text
    assert "include @codex-device-a in your Hub reply" in prompt_text


def test_runner_auto_executes_task_and_writes_result_to_hub(tmp_path):
    client = FakeHubClient(
        {
            "device": {"label": "codex-device-a"},
            "tasks": [
                {
                    "id": 42,
                    "project_slug": "private-server-buildout",
                    "project_name": "Private Server Buildout",
                    "title": "Check bridge",
                    "priority": "low",
                    "status": "open",
                }
            ],
            "mentions": [],
        }
    )
    launcher = FakeLauncher()
    executor = FakeExecutor(summary="自动执行结果。")
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    cli_path = tmp_path / "codex.cmd"
    cli_path.write_text("@echo off", encoding="utf-8")
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={"private-server-buildout": project_dir},
        ignore_existing_on_first_run=False,
        auto_execute_codex=True,
        codex_cli_path=cli_path,
    )

    result = BridgeRunner(
        config, client=client, launcher=launcher, executor=executor
    ).process_once()

    assert result.claimed_tasks == [42]
    assert len(result.executed_codex) == 1
    assert executor.calls
    assert executor.calls[0][0] == "private-server-buildout"
    assert "Task 42 in private-server-buildout" in executor.calls[0][3]
    assert launcher.urls == []
    assert client.task_status_updates[-1][:3] == (
        "private-server-buildout",
        42,
        "review",
    )
    assert "自动执行结果" in client.task_status_updates[-1][3]
    assert len(client.task_entries) == 1
    assert "CodexBridge 自动执行任务 #42" in client.task_entries[0][2]
    assert "自动执行结果" in client.task_entries[0][2]


def test_initial_task_result_does_not_summon_another_device(tmp_path):
    client = FakeHubClient(
        {
            "device": {"label": "hermes-device-a"},
            "tasks": [
                {
                    "id": 44,
                    "project_slug": "private-server-buildout",
                    "project_name": "Private Server Buildout",
                    "primary_device_label": "codex-device-a",
                    "title": "Review as assistant",
                    "priority": "low",
                    "status": "open",
                }
            ],
            "mentions": [],
        }
    )
    executor = FakeExecutor(summary="assistant suggestion")
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    cli_path = tmp_path / "codex.cmd"
    cli_path.write_text("@echo off", encoding="utf-8")
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={"private-server-buildout": project_dir},
        ignore_existing_on_first_run=False,
        auto_execute_codex=True,
        codex_cli_path=cli_path,
    )

    BridgeRunner(
        config, client=client, launcher=FakeLauncher(), executor=executor
    ).process_once()

    assert not client.task_entries[0][2].startswith("@codex-device-a")
    assert client.task_entry_reply_ids == [None]


def test_runner_task_target_project_slug_auto_creates_missing_project(tmp_path):
    client = FakeHubClient(
        {
            "device": {"label": "codex-device-a"},
            "tasks": [
                {
                    "id": 43,
                    "project_slug": "private-server-buildout",
                    "project_name": "Private Server Buildout",
                    "title": "Open target workspace",
                    "priority": "normal",
                    "status": "open",
                }
            ],
            "mentions": [],
            "task_prompt": "\n".join(
                [
                    "# Codex Task",
                    "",
                    "Codex target project slug: zbcs",
                    "请检查现有 skill。",
                ]
            ),
        }
    )
    launcher = FakeLauncher()
    private_server_dir = tmp_path / "private-server"
    private_server_dir.mkdir()
    project_root = tmp_path / "CodexProjects"
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={"private-server-buildout": private_server_dir},
        project_root=project_root,
        auto_create_missing_projects=True,
        ignore_existing_on_first_run=False,
    )

    result = BridgeRunner(config, client=client, launcher=launcher).process_once()

    assert result.claimed_tasks == [43]
    assert (project_root / "zbcs").is_dir()
    assert launcher.urls
    params = parse_qs(urlparse(launcher.urls[0]).query)
    assert params["path"] == [str((project_root / "zbcs").resolve())]
    assert "Codex target project slug: zbcs" in params["prompt"][0]


def test_runner_auto_executes_mention_and_marks_handled(tmp_path):
    client = FakeHubClient(
        {
            "device": {"label": "codex-device-a"},
            "tasks": [],
            "mentions": [
                {
                    "id": 7,
                    "project_slug": "private-server-buildout",
                    "project_name": "Private Server Buildout",
                    "source_label": "BYBC",
                    "body_markdown": "@codex-device-a please check this",
                }
            ],
        }
    )
    launcher = FakeLauncher()
    executor = FakeExecutor(summary="提及处理完成。")
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    cli_path = tmp_path / "codex.cmd"
    cli_path.write_text("@echo off", encoding="utf-8")
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={"private-server-buildout": project_dir},
        ignore_existing_on_first_run=False,
        auto_execute_codex=True,
        codex_cli_path=cli_path,
    )

    result = BridgeRunner(
        config, client=client, launcher=launcher, executor=executor
    ).process_once()

    assert result.claimed_mentions == [7]
    assert len(result.executed_codex) == 1
    assert launcher.urls == []
    assert client.handled_mentions == [("private-server-buildout", 7)]
    assert len(client.entries) == 1
    assert "CodexBridge 自动执行提及 #7" in client.entries[0][1]
    assert "提及处理完成" in client.entries[0][1]


def test_entry_execution_preferences_are_backend_scoped_and_validated():
    entry = {
        "metadata_json": (
            '{"execution":{"codex":{"model":"gpt-5.6-sol",'
            '"reasoning_effort":"ultra"},"hermes":{"model":"nous/hermes",'
            '"reasoning_effort":"max"}}}'
        )
    }

    assert execution_preferences_for_entry(entry, "codex") == {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "ultra",
    }
    assert execution_preferences_for_entry(entry, "hermes") == {
        "model": "nous/hermes",
        "reasoning_effort": "max",
    }
    assert execution_preferences_for_entry(
        {"metadata_json": '{"execution":{"codex":{"model":"bad value"}}}'},
        "codex",
    ) == {}
    assert execution_preferences_for_entry({"metadata_json": "not-json"}, "codex") == {}


def test_runner_passes_mention_execution_preferences_to_selected_backend(tmp_path):
    client = FakeHubClient(
        {
            "device": {"label": "codex-device-a"},
            "tasks": [],
            "mentions": [
                {
                    "id": 8,
                    "project_slug": "private-server-buildout",
                    "project_name": "Private Server Buildout",
                    "source_label": "BYBC",
                    "body_markdown": "@codex-device-a use my selection",
                    "metadata_json": (
                        '{"execution":{"codex":{"model":"gpt-5.6-luna",'
                        '"reasoning_effort":"high"}}}'
                    ),
                }
            ],
        }
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    executor = FakeExecutor()
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={"private-server-buildout": project_dir},
        ignore_existing_on_first_run=False,
        auto_execute_codex=True,
        execution_backend="codex",
    )

    BridgeRunner(config, client=client, executor=executor).process_once()

    assert executor.calls[0][4] == {
        "model": "gpt-5.6-luna",
        "reasoning_effort": "high",
    }


def test_runner_launches_requested_project_thread_outside_codex_exec(tmp_path):
    client = FakeHubClient(
        {
            "device": {"label": "codex-device-a"},
            "tasks": [],
            "mentions": [
                {
                    "id": 21,
                    "project_slug": "private-server-buildout",
                    "project_name": "Private Server Buildout",
                    "source_label": "BYBC",
                    "body_markdown": "@codex-device-a 在zbcs项目新建一个对话，内容为检查现有的skill",
                }
            ],
        }
    )
    launcher = FakeLauncher()
    executor = FakeExecutor(summary="should not run")
    private_server_dir = tmp_path / "private-server"
    private_server_dir.mkdir()
    zbcs_dir = tmp_path / "zbcs"
    zbcs_dir.mkdir()
    cli_path = tmp_path / "codex.cmd"
    cli_path.write_text("@echo off", encoding="utf-8")
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={
            "private-server-buildout": private_server_dir,
            "zbcs": zbcs_dir,
        },
        ignore_existing_on_first_run=False,
        auto_execute_codex=True,
        codex_cli_path=cli_path,
    )

    result = BridgeRunner(
        config, client=client, launcher=launcher, executor=executor
    ).process_once()

    assert result.claimed_mentions == [21]
    assert result.executed_codex == []
    assert executor.calls == []
    assert launcher.urls
    launch = urlparse(launcher.urls[0])
    params = parse_qs(launch.query)
    assert launch.scheme == "codex"
    assert params["path"] == [str(zbcs_dir.resolve())]
    assert params["prompt"] == ["检查现有的skill"]
    assert client.handled_mentions == [("private-server-buildout", 21)]
    assert "已打开 Codex 新对话" in client.entries[0][1]


def test_load_config_accepts_powershell_utf8_bom(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        '{"hub":"http://hub.example/hub","token":"secret","projects":{}}',
        encoding="utf-8-sig",
    )

    config = load_config(path)

    assert config.hub == "http://hub.example/hub"
    assert config.token == "secret"


def test_sample_config_defaults_to_cmynetwork_and_chatgpt_health_check():
    config = sample_config()

    assert config["vpn_name"] == "cmynetwork"
    assert config["vpn_health_url"] == "https://chatgpt.com/"
    assert config["vpn_auto_recover"] is False
    assert config["auto_submit_codex_prompt"] is False
    assert config["codex_submit_delay_seconds"] == 3.0
    assert config["codex_submit_hotkey"] == "enter"
    assert config["auto_execute_codex"] is False
    assert config["codex_exec_sandbox"] == "workspace-write"
    assert config["codex_exec_ignore_user_config"] is True
    assert config["auto_create_missing_projects"] is True
    assert "CodexProjects" in config["project_root"]
    assert "CMYNetwork" in config["vpn_exe_path"]


def test_save_config_preserves_token_and_writes_local_tool_paths(tmp_path):
    path = tmp_path / "config.json"
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={"private-server-buildout": tmp_path / "project"},
        codex_exe_path=tmp_path / "Codex.exe",
        auto_submit_codex_prompt=True,
        codex_submit_delay_seconds=1.5,
        codex_submit_hotkey="ctrl-enter",
        auto_execute_codex=True,
        codex_cli_path=tmp_path / "codex.cmd",
        vpn_exe_path=tmp_path / "cmynetwork.exe",
        vpn_auto_recover=True,
    )

    save_config(config, path)
    loaded = load_config(path)

    assert loaded.token == "secret"
    assert loaded.codex_exe_path == tmp_path / "Codex.exe"
    assert loaded.auto_submit_codex_prompt is True
    assert loaded.codex_submit_delay_seconds == 1.5
    assert loaded.codex_submit_hotkey == "ctrl-enter"
    assert loaded.auto_execute_codex is True
    assert loaded.codex_cli_path == tmp_path / "codex.cmd"
    assert loaded.project_root == Path.home() / "Documents" / "CodexProjects"
    assert loaded.auto_create_missing_projects is True
    assert loaded.vpn_name == "cmynetwork"
    assert loaded.vpn_exe_path == tmp_path / "cmynetwork.exe"
    assert loaded.vpn_auto_recover is True


def test_update_local_tool_settings_normalizes_blank_paths(tmp_path):
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={},
        codex_exe_path=tmp_path / "old-codex.exe",
        codex_cli_path=tmp_path / "old-codex.cmd",
        vpn_exe_path=tmp_path / "old-vpn.exe",
    )

    update_local_tool_settings(
        config,
        codex_exe_path="",
        auto_submit_codex_prompt=True,
        codex_submit_delay_seconds=0.5,
        codex_submit_hotkey="enter",
        auto_execute_codex=True,
        codex_cli_path=tmp_path / "codex.cmd",
        codex_exec_sandbox="read-only",
        codex_exec_timeout_seconds=60,
        vpn_exe_path=tmp_path / "cmynetwork.exe",
        vpn_health_url="https://chatgpt.com/",
        vpn_auto_recover=False,
        vpn_switch_command="",
    )

    assert config.codex_exe_path is None
    assert config.auto_submit_codex_prompt is True
    assert config.codex_submit_delay_seconds == 0.5
    assert config.codex_submit_hotkey == "enter"
    assert config.auto_execute_codex is True
    assert config.codex_cli_path == tmp_path / "codex.cmd"
    assert config.codex_exec_sandbox == "read-only"
    assert config.codex_exec_timeout_seconds == 60
    assert config.vpn_exe_path == tmp_path / "cmynetwork.exe"
    assert config.vpn_health_url == "https://chatgpt.com/"
    assert config.vpn_auto_recover is False
    assert config.vpn_switch_command == ""


def allow_test_cli_instance(monkeypatch):
    class TestSingleInstance:
        def acquire(self):
            return True

        def close(self):
            return None

    monkeypatch.setattr(cli, "SingleInstance", TestSingleInstance)


def test_cli_once_returns_nonzero_when_hub_is_unreachable(tmp_path, monkeypatch):
    allow_test_cli_instance(monkeypatch)
    path = tmp_path / "config.json"
    path.write_text(
        '{"hub":"http://hub.example/hub","token":"secret","projects":{}}',
        encoding="utf-8",
    )

    class FailingRunner:
        def __init__(self, config):
            self.config = config

        def process_once(self):
            raise HubClientError("offline")

    monkeypatch.setattr(cli, "BridgeRunner", FailingRunner)

    assert cli.main(["--config", str(path), "--once"]) == 1


def test_cli_without_mode_launches_gui(monkeypatch):
    allow_test_cli_instance(monkeypatch)
    launched = []

    def fake_gui_main(config_path=None, auto_start=False):
        launched.append((config_path, auto_start))
        return 0

    monkeypatch.setattr(cli, "gui_main", fake_gui_main)

    assert cli.main([]) == 0
    assert launched


def test_cli_start_launches_gui_with_auto_start(monkeypatch):
    allow_test_cli_instance(monkeypatch)
    launched = []

    def fake_gui_main(config_path=None, auto_start=False):
        launched.append((config_path, auto_start))
        return 0

    monkeypatch.setattr(cli, "gui_main", fake_gui_main)

    assert cli.main(["--start"]) == 0
    assert launched[0][1] is True


def test_runner_mirrors_project_memory_and_injects_it_into_new_codex_item(
    tmp_path,
):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "notes.md").write_text("shared project note", encoding="utf-8")
    memory = "\n".join(
        [
            "# Project Memory: Private Server Buildout",
            "",
            "Memory version: 4",
            "",
            "## Decisions",
            "Create a fresh local Codex conversation for every item.",
            "",
            "## Memory Use",
            "Verbose mirror-only guidance.",
        ]
    )
    runtime_memory = "\n".join(
        [
            "# Project Context: Private Server Buildout",
            "Memory version: 4",
            "Decisions: Create a fresh local Codex conversation for every item.",
        ]
    )
    client = FakeHubClient(
        {
            "device": {"label": "codex-device-a"},
            "tasks": [],
            "mentions": [
                {
                    "id": 77,
                    "project_slug": "private-server-buildout",
                    "project_name": "Private Server Buildout",
                    "source_label": "BYBC",
                    "body_markdown": "@codex-device-a continue the project",
                }
            ],
            "project_memory": memory,
            "project_runtime_memory": runtime_memory,
        }
    )
    launcher = FakeLauncher()
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={"private-server-buildout": project_dir},
        ignore_existing_on_first_run=False,
        open_codex=True,
    )

    result = BridgeRunner(config, client=client, launcher=launcher).process_once()

    memory_path = project_dir / ".codex-hub" / "PROJECT_MEMORY.md"
    sync_path = project_dir / ".codex-hub" / "sync.json"
    launched_prompt = parse_qs(urlparse(launcher.urls[0]).query)["prompt"][0]
    assert result.claimed_mentions == [77]
    assert memory_path.read_text(encoding="utf-8").startswith("# Project Memory")
    assert "Verbose mirror-only guidance" in memory_path.read_text(encoding="utf-8")
    assert sync_path.exists()
    assert "Create a fresh local Codex conversation" in launched_prompt
    assert "Verbose mirror-only guidance" not in launched_prompt
    assert launched_prompt.index("## Durable Project Memory") < launched_prompt.index(
        "## Message"
    )
    assert client.memory_requests == ["private-server-buildout"]
    assert client.runtime_memory_requests == ["private-server-buildout"]
    assert client.sync_snapshots[0][1] == 4
    assert any(
        item["path"] == ".codex-hub/PROJECT_MEMORY.md"
        for item in client.sync_snapshots[0][3]["files"]
    )


def test_runner_rejects_corrupt_remote_memory_and_keeps_valid_local_copy(tmp_path):
    project_dir = tmp_path / "project"
    memory_dir = project_dir / ".codex-hub"
    memory_dir.mkdir(parents=True)
    valid_memory = "\n".join(
        [
            "# Project Memory: Private Server Buildout",
            "Memory version: 3",
            "## Constraints",
            "Preserve UTF-8 text.",
        ]
    )
    memory_path = memory_dir / "PROJECT_MEMORY.md"
    memory_path.write_text(valid_memory, encoding="utf-8")
    damaged = (
        "# Project Memory: Private Server Buildout\n"
        "Memory version: 4\n"
        "## Goal\n????????????????????\n"
        "## Constraints\n????????????????????"
    )
    client = FakeHubClient(
        {
            "device": {"label": "codex-device-a"},
            "tasks": [],
            "mentions": [
                {
                    "id": 78,
                    "project_slug": "private-server-buildout",
                    "project_name": "Private Server Buildout",
                    "source_label": "BYBC",
                    "body_markdown": "@codex-device-a inspect the project",
                }
            ],
            "project_memory": damaged,
            "project_runtime_memory": damaged,
        }
    )
    launcher = FakeLauncher()
    config = BridgeConfig(
        hub="http://hub.example/hub",
        token="secret",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        projects={"private-server-buildout": project_dir},
        ignore_existing_on_first_run=False,
        open_codex=True,
    )

    result = BridgeRunner(config, client=client, launcher=launcher).process_once()

    launched_prompt = parse_qs(urlparse(launcher.urls[0]).query)["prompt"][0]
    assert memory_path.read_text(encoding="utf-8") == valid_memory
    assert "Preserve UTF-8 text" in launched_prompt
    assert "????????" not in launched_prompt
    assert any("本地正常副本" in message for message in result.messages)


@pytest.mark.parametrize("task_id", [7, None])
def test_selected_device_executes_independently_and_links_its_reply(tmp_path, task_id):
    mention = {
        "id": 303, "project_slug": "project-one", "project_name": "Project",
        "task_id": task_id,
        "source_label": "user", "body_markdown": "Only run this task",
        "delivery_mode": "selected", "primary_device_label": "codex-device-a",
        "delivery_reason": "collaboration", "collaboration_role": "advisor",
        "group_chat_roles": [{"name": "critic", "instructions": "Review everyone"}],
    }
    client = FakeHubClient({"device": {"label": "hermes-device-a"}, "tasks": [], "mentions": [mention]})
    config = BridgeConfig(hub="https://hub.example", token="test", prompt_dir=tmp_path / "prompts", state_path=tmp_path / "state.json", projects={"project-one": tmp_path / "project"}, auto_execute_codex=True, ignore_existing_on_first_run=False)
    executor = FakeExecutor(summary="Done")
    runner = BridgeRunner(config, client=client, launcher=FakeLauncher(), executor=executor)
    result = runner.process_once()
    prompt = result.prompt_files[0].read_text(encoding="utf8")
    assert "Execute independently" in prompt
    assert "advisory collaborator" not in prompt
    assert "Assigned Group Chat Roles" not in prompt
    assert "@codex-device-a" not in prompt
    assert "--mention-id 303" in prompt
    if task_id is None:
        assert not client.entries[0][1].startswith("@codex-device-a")
        assert client.entry_reply_ids == [303]
        assert client.task_entries == []
    else:
        assert "--task-id 7" in prompt
        assert not client.task_entries[0][2].startswith("@codex-device-a")
        assert client.task_entries[0][:2] == ("project-one", 7)
        assert client.task_entry_reply_ids == [303]
        assert client.entries == []


def test_long_chinese_prompt_uses_complete_local_file_before_url_expansion(tmp_path):
    from urllib.parse import urlencode
    config = BridgeConfig(hub="https://hub.example", token="test", prompt_dir=tmp_path, state_path=tmp_path / "state.json")
    runner = BridgeRunner(config, client=FakeHubClient({}), executor=FakeExecutor())
    body = "中文任务内容" * 500
    path = tmp_path / "mention-303-project-one.md"
    path.write_text(body, encoding="utf8")
    prompt = runner._build_codex_launch_prompt(path, body)
    assert "请读取并执行这个文件内容" in prompt
    assert str(path) in prompt
    assert len(urlencode({"prompt": prompt})) < 6000
    assert path.read_text(encoding="utf8") == body


def test_capabilities_report_is_cached_and_older_hub_does_not_block_inbox(tmp_path):
    client = FakeHubClient({"device": {"label": "codex-device-a"}, "mentions": [], "tasks": []})
    reports = []
    client.post_device_capabilities = reports.append
    config = BridgeConfig(hub="https://hub.example", token="test", prompt_dir=tmp_path, state_path=tmp_path / "state.json")
    runner = BridgeRunner(config, client=client, executor=FakeExecutor())
    runner._model_catalog.get_catalog = lambda: {"models": [{"model": "gpt-6-astra"}]}
    runner.poll_status()
    runner.poll_status()
    assert reports == [{"execution_backend": "codex", "execution_mode": "codex-desktop", "codex": {"models": [{"model": "gpt-6-astra"}]}}]
    runner._next_capability_report = 0
    client.post_device_capabilities = lambda _: (_ for _ in ()).throw(RuntimeError("404 old Hub"))
    assert runner.poll_status().device_label == "codex-device-a"
