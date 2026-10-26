from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from codex_hub.config import Settings
from codex_hub.db import connect_database, initialize_schema
from codex_hub.main import create_app, entry_with_attachments
from codex_hub.repository import Repository
from codex_hub.server_ai import (
    CompletionResult,
    OpenAICompatibleCompletionClient,
    ProviderConfig,
    ProviderConfigurationError,
    ProviderRequestError,
    ServerAIRuntime,
    completion_endpoint,
    is_deepseek_peak_time,
)


class FakeCompletionClient:
    def __init__(self, result: CompletionResult | Exception):
        self.result = result
        self.messages: list[dict[str, str]] | None = None

    def complete(self, messages: list[dict[str, str]]) -> CompletionResult:
        self.messages = messages
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def make_server_ai_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "data_dir": tmp_path / "state",
        "secret_key": "test-secret",
        "owner_username": "owner",
        "owner_password": "owner-password",
        "server_ai_enabled": True,
        "deepseek_api_key": "ds-secret-key",
        "deepseek_model": "deepseek-v4-pro",
        "kimi_api_key": "",
    }
    values.update(overrides)
    return Settings(**values)


def initialize_repo(settings: Settings) -> Repository:
    settings.ensure_directories()
    connection = connect_database(settings.database_path)
    initialize_schema(connection)
    return Repository(connection)


def seed_project(repo: Repository, slug: str = "server-ai") -> tuple[dict, dict]:
    owner = repo.create_owner_if_missing("owner", "owner-password")
    local = repo.create_device(label=f"{slug}-device")
    project = repo.create_project(
        slug=slug,
        name="Server AI",
        created_by_device_id=local["id"],
    )
    return owner, project


def test_provider_config_repr_never_contains_api_key(tmp_path):
    settings = make_server_ai_settings(tmp_path)
    runtime = ServerAIRuntime(settings)

    assert "ds-secret-key" not in repr(settings)
    assert "ds-secret-key" not in repr(runtime.providers["deepseek"])


def test_provider_endpoint_is_pinned_to_official_https_hosts():
    official = ProviderConfig(
        slug="deepseek",
        label="DeepSeek",
        api_key="secret",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-v4-pro",
    )
    custom = ProviderConfig(
        slug="deepseek",
        label="DeepSeek",
        api_key="secret",
        base_url="https://proxy.example/v1",
        model="deepseek-v4-pro",
    )

    assert completion_endpoint(official) == "https://api.deepseek.com/v1/chat/completions"
    with pytest.raises(ProviderConfigurationError):
        completion_endpoint(custom)
    assert completion_endpoint(
        custom, allow_custom_base_urls=True
    ) == "https://proxy.example/v1/chat/completions"

    kimi_coding = ProviderConfig(
        slug="kimi",
        label="Kimi",
        api_key="secret",
        base_url="https://api.kimi.com/coding/v1",
        model="k3",
    )
    assert completion_endpoint(kimi_coding) == (
        "https://api.kimi.com/coding/v1/chat/completions"
    )


@pytest.mark.parametrize(
    ("utc_hour", "utc_minute", "expected"),
    [
        (0, 59, False),
        (1, 0, True),
        (3, 59, True),
        (4, 0, False),
        (5, 59, False),
        (6, 0, True),
        (9, 59, True),
        (10, 0, False),
    ],
)
def test_deepseek_peak_time_uses_beijing_boundaries(
    utc_hour, utc_minute, expected
):
    moment = datetime(2026, 8, 18, utc_hour, utc_minute, tzinfo=timezone.utc)
    assert is_deepseek_peak_time(moment) is expected


def test_openai_compatible_client_sends_bearer_key_and_parses_usage(tmp_path):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["Authorization"]
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4-pro",
                "choices": [{"message": {"content": "完成。"}}],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 3,
                    "total_tokens": 15,
                },
            },
        )

    settings = make_server_ai_settings(tmp_path)
    config = ServerAIRuntime(settings).providers["deepseek"]
    client = OpenAICompatibleCompletionClient(
        config, settings, transport=httpx.MockTransport(handler)
    )

    result = client.complete([{"role": "user", "content": "测试"}])

    assert seen["authorization"] == "Bearer ds-secret-key"
    assert seen["payload"]["stream"] is False
    assert result.content == "完成。"
    assert result.usage["total_tokens"] == 15


def test_repository_enqueues_only_allowlisted_exact_mentions_and_is_idempotent(repo):
    owner, project = seed_project(repo, "exact-mention")
    deepseek = repo.ensure_server_ai_device("deepseek", "DeepSeek")
    repo.add_project_member(project["id"], deepseek["id"], "write")
    near_miss = repo.create_entry(
        project_id=project["id"],
        author_user_id=owner["id"],
        source_label="owner",
        kind="message",
        body_markdown="@DeepSeekExtra 不应触发",
    )
    exact = repo.create_entry(
        project_id=project["id"],
        author_user_id=owner["id"],
        source_label="owner",
        kind="message",
        body_markdown="请 @DeepSeek 审阅",
    )

    assert repo.enqueue_server_ai_jobs_for_entry(near_miss["id"], {"deepseek"}) == []
    first = repo.enqueue_server_ai_jobs_for_entry(
        exact["id"], {"deepseek"}, {"deepseek": "deepseek-v4-pro"}
    )
    second = repo.enqueue_server_ai_jobs_for_entry(
        exact["id"], {"deepseek"}, {"deepseek": "deepseek-v4-pro"}
    )

    assert len(first) == 1
    assert first[0]["status"] == "queued"
    assert second[0]["id"] == first[0]["id"]
    assert len(repo.list_server_ai_jobs(project["id"])) == 1


def test_server_ai_runtime_writes_one_bounded_text_response(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "codex_hub.server_ai.is_deepseek_peak_time", lambda _moment=None: True
    )
    settings = make_server_ai_settings(tmp_path)
    repo = initialize_repo(settings)
    try:
        owner, project = seed_project(repo, "runtime-success")
        fake = FakeCompletionClient(
            CompletionResult(
                content="已完成服务端审阅。",
                model="deepseek-v4-pro",
                usage={"total_tokens": 42},
            )
        )
        runtime = ServerAIRuntime(settings, client_factory=lambda *_args: fake)
        runtime.prepare(repo)
        deepseek = repo.get_server_ai_device("deepseek")
        repo.add_project_member(project["id"], deepseek["id"], "write")
        entry = repo.create_entry(
            project_id=project["id"],
            author_user_id=owner["id"],
            source_label="owner",
            kind="message",
            body_markdown="@DeepSeek 检查这项设计。",
        )
        jobs = runtime.enqueue_entry(repo, entry)

        assert jobs[0]["status"] == "queued"
        assert runtime.run_once() is True
        assert runtime.run_once() is False

        job = repo.get_server_ai_job(jobs[0]["id"])
        response = repo.get_entry(job["response_entry_id"])
        metadata = json.loads(response["metadata_json"])["server_ai"]
        assert job["status"] == "succeeded"
        assert response["source_label"] == "DeepSeek"
        assert response["body_markdown"] == "已完成服务端审阅。"
        assert metadata["usage"] == {"total_tokens": 42}
        assert metadata["peak_call"] is True
        assert metadata["consumption_multiplier"] == 2
        assert metadata["billing_timezone"] == "Asia/Shanghai"
        decorated = entry_with_attachments(
            response, [], files_root=settings.files_dir
        )
        assert decorated["server_ai_peak_call"] is True
        assert "@DeepSeek 检查这项设计。" in fake.messages[1]["content"]
    finally:
        repo.connection.close()


def test_missing_key_stays_awaiting_without_constructing_a_client(tmp_path):
    settings = make_server_ai_settings(tmp_path, deepseek_api_key="")
    repo = initialize_repo(settings)
    constructed = []
    try:
        owner, project = seed_project(repo, "awaiting-key")
        runtime = ServerAIRuntime(
            settings,
            client_factory=lambda *_args: constructed.append(True),
        )
        runtime.prepare(repo)
        deepseek = repo.get_server_ai_device("deepseek")
        repo.add_project_member(project["id"], deepseek["id"], "write")
        entry = repo.create_entry(
            project_id=project["id"],
            author_user_id=owner["id"],
            source_label="owner",
            kind="message",
            body_markdown="@DeepSeek 等待密钥。",
        )
        job = runtime.enqueue_entry(repo, entry)[0]

        assert job["status"] == "awaiting_key"
        assert runtime.run_once() is False
        assert constructed == []
    finally:
        repo.connection.close()


def test_retry_error_redacts_key_and_does_not_publish_early(tmp_path):
    settings = make_server_ai_settings(tmp_path, server_ai_max_attempts=3)
    repo = initialize_repo(settings)
    try:
        owner, project = seed_project(repo, "retry-redaction")
        fake = FakeCompletionClient(
            ProviderRequestError(
                "temporary failure includes ds-secret-key", retryable=True
            )
        )
        runtime = ServerAIRuntime(settings, client_factory=lambda *_args: fake)
        runtime.prepare(repo)
        deepseek = repo.get_server_ai_device("deepseek")
        repo.add_project_member(project["id"], deepseek["id"], "write")
        entry = repo.create_entry(
            project_id=project["id"],
            author_user_id=owner["id"],
            source_label="owner",
            kind="message",
            body_markdown="@DeepSeek retry",
        )
        job = runtime.enqueue_entry(repo, entry)[0]

        assert runtime.run_once() is True
        retried = repo.get_server_ai_job(job["id"])
        assert retried["status"] == "retrying"
        assert "ds-secret-key" not in retried["last_error"]
        assert "[REDACTED]" in retried["last_error"]
        assert retried["response_entry_id"] is None
    finally:
        repo.connection.close()


def test_daily_request_ceiling_leaves_extra_work_queued(tmp_path):
    settings = make_server_ai_settings(
        tmp_path, server_ai_max_requests_per_day=1
    )
    repo = initialize_repo(settings)
    try:
        owner, project = seed_project(repo, "daily-ceiling")
        fake = FakeCompletionClient(
            CompletionResult("第一项完成。", "deepseek-v4-pro", {"total_tokens": 8})
        )
        runtime = ServerAIRuntime(settings, client_factory=lambda *_args: fake)
        runtime.prepare(repo)
        deepseek = repo.get_server_ai_device("deepseek")
        repo.add_project_member(project["id"], deepseek["id"], "write")
        for body in ("@DeepSeek 第一项", "@DeepSeek 第二项"):
            entry = repo.create_entry(
                project_id=project["id"],
                author_user_id=owner["id"],
                source_label="owner",
                kind="message",
                body_markdown=body,
            )
            runtime.enqueue_entry(repo, entry)

        assert runtime.run_once() is True
        assert runtime.run_once() is False
        jobs = list(reversed(repo.list_server_ai_jobs(project["id"])))
        assert [job["status"] for job in jobs] == ["succeeded", "queued"]
        status = runtime.statuses(repo, project["id"])[0]
        assert status["state"] == "quota_exhausted"
        assert status["requests_today"] == 1
    finally:
        repo.connection.close()


def test_web_owner_can_enable_provider_and_queue_before_key(tmp_path):
    settings = make_server_ai_settings(tmp_path, deepseek_api_key="")
    app = create_app(settings)
    repo = app.state.repo
    owner, project = seed_project(repo, "web-server-ai")
    with TestClient(app) as client:
        login = client.post(
            "/login",
            data={"username": "owner", "password": "owner-password"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        csrf = client.cookies[settings.csrf_cookie_name]
        enabled = client.post(
            "/projects/web-server-ai/server-ai/deepseek",
            data={"_csrf": csrf, "enabled": "1"},
            follow_redirects=False,
        )
        queued = client.post(
            "/projects/web-server-ai/entries",
            data={
                "_csrf": csrf,
                "kind": "message",
                "body_markdown": "@DeepSeek 密钥到位后执行。",
            },
            follow_redirects=False,
        )
        status = client.get("/api/server-ai/status")
        page = client.get("/projects/web-server-ai")

    jobs = repo.list_server_ai_jobs(project["id"])
    assert enabled.status_code == 303
    assert queued.status_code == 303
    assert jobs[0]["status"] == "awaiting_key"
    assert status.json()["providers"][0]["state"] == "awaiting_key"
    assert "服务器 AI 直连" in page.text
    assert "等待 API Key" in page.text
    assert "@DeepSeek 密钥到位后执行。" in page.text
