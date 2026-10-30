from codex_hub.static_assets import asset_url
from codex_hub.main import BASE_DIR
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient
from PIL import Image

from codex_hub.repository import Repository


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def login_owner(client) -> str:
    response = client.post(
        "/login",
        data={"username": "owner", "password": "owner-password"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    csrf = client.cookies.get("codex_hub_csrf")
    assert csrf
    return csrf


def test_api_rejects_missing_token(client):
    response = client.get("/api/projects")

    assert response.status_code == 401


def test_api_rejects_malformed_and_non_object_json_without_500(client, repo):
    device = repo.create_device(label="json-device")
    token = repo.create_api_token(device_id=device["id"], name="main")
    headers = {**auth_header(token.raw), "Content-Type": "application/json"}

    malformed = client.post("/api/projects", headers=headers, content=b"{")
    array_payload = client.post("/api/projects", headers=headers, json=[])

    assert malformed.status_code == 400
    assert malformed.json()["detail"] == "request body must be valid JSON"
    assert array_payload.status_code == 400
    assert array_payload.json()["detail"] == "request body must be a JSON object"


def test_token_can_create_and_list_own_project(client, repo):
    device = repo.create_device(label="device-a")
    token = repo.create_api_token(device_id=device["id"], name="main")

    response = client.post(
        "/api/projects",
        headers=auth_header(token.raw),
        json={"slug": "plan", "name": "Plan", "description": "Shared plan"},
    )
    listed = client.get("/api/projects", headers=auth_header(token.raw))

    assert response.status_code == 201
    assert listed.json()["projects"][0]["slug"] == "plan"


def test_blocked_device_cannot_read_or_write_entries(client, repo):
    allowed = repo.create_device(label="allowed")
    blocked = repo.create_device(label="blocked")
    allowed_token = repo.create_api_token(device_id=allowed["id"], name="main")
    blocked_token = repo.create_api_token(device_id=blocked["id"], name="main")
    repo.create_project(slug="sealed", name="Sealed", created_by_device_id=allowed["id"])

    read_response = client.get(
        "/api/projects/sealed/entries",
        headers=auth_header(blocked_token.raw),
    )
    write_response = client.post(
        "/api/projects/sealed/entries",
        headers=auth_header(blocked_token.raw),
        json={"kind": "message", "body_markdown": "should not land"},
    )
    allowed_response = client.post(
        "/api/projects/sealed/entries",
        headers=auth_header(allowed_token.raw),
        json={"kind": "markdown", "body_markdown": "# Context"},
    )

    assert read_response.status_code == 403
    assert write_response.status_code == 403
    assert allowed_response.status_code == 201


def test_device_entry_round_trips_unicode_and_rejects_question_mark_loss(client, repo):
    device = repo.create_device(label="device-utf8")
    token = repo.create_api_token(device_id=device["id"], name="main")
    repo.create_project(
        slug="unicode-safe",
        name="Unicode Safe",
        created_by_device_id=device["id"],
    )

    unicode_response = client.post(
        "/api/projects/unicode-safe/entries",
        headers=auth_header(token.raw),
        json={"kind": "message", "body_markdown": "中文协作测试"},
    )
    damaged_response = client.post(
        "/api/projects/unicode-safe/entries",
        headers=auth_header(token.raw),
        json={
            "kind": "message",
            "body_markdown": (
                "?????Hermes ?????????????????? --model k3 ????????\n"
                "???????? Codex ?????????????"
            ),
        },
    )

    assert unicode_response.status_code == 201
    assert unicode_response.json()["entry"]["body_markdown"] == "中文协作测试"
    assert damaged_response.status_code == 422
    assert "non-ASCII text" in damaged_response.json()["detail"]


def test_entry_api_stores_validated_execution_metadata(client, repo):
    device = repo.create_device(label="model-device")
    token = repo.create_api_token(device_id=device["id"], name="main")
    repo.create_project(
        slug="model-routing",
        name="Model Routing",
        created_by_device_id=device["id"],
    )

    created = client.post(
        "/api/projects/model-routing/entries",
        headers=auth_header(token.raw),
        json={
            "kind": "message",
            "body_markdown": "use selected runtimes",
            "execution": {
                "codex": {
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "ultra",
                },
                "hermes": {
                    "model": "nous/hermes-4",
                    "reasoning_effort": "max",
                },
            },
        },
    )
    rejected = client.post(
        "/api/projects/model-routing/entries",
        headers=auth_header(token.raw),
        json={
            "kind": "message",
            "body_markdown": "bad override",
            "execution": {"codex": {"model": "unsafe model value"}},
        },
    )

    assert created.status_code == 201
    metadata = json.loads(created.json()["entry"]["metadata_json"])
    assert metadata["execution"]["codex"] == {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "ultra",
    }
    assert metadata["execution"]["hermes"] == {
        "model": "nous/hermes-4",
        "reasoning_effort": "max",
    }
    assert rejected.status_code == 422


def test_attachment_download_requires_project_membership(client, repo):
    allowed = repo.create_device(label="allowed")
    blocked = repo.create_device(label="blocked")
    allowed_token = repo.create_api_token(device_id=allowed["id"], name="main")
    blocked_token = repo.create_api_token(device_id=blocked["id"], name="main")
    repo.create_project(slug="files", name="Files", created_by_device_id=allowed["id"])

    upload = client.post(
        "/api/projects/files/attachments",
        headers=auth_header(allowed_token.raw),
        files={"file": ("note.txt", b"hello", "text/plain")},
    )
    attachment_id = upload.json()["attachment"]["id"]
    blocked_download = client.get(
        f"/api/projects/files/attachments/{attachment_id}",
        headers=auth_header(blocked_token.raw),
    )

    assert upload.status_code == 201
    assert blocked_download.status_code == 403


def test_export_contains_timeline_without_token_material(client, repo):
    device = repo.create_device(label="device-a")
    token = repo.create_api_token(device_id=device["id"], name="main")
    repo.create_project(slug="exportable", name="Exportable", created_by_device_id=device["id"])
    client.post(
        "/api/projects/exportable/entries",
        headers=auth_header(token.raw),
        json={"kind": "markdown", "body_markdown": "shared context"},
    )

    response = client.get(
        "/api/projects/exportable/export.md",
        headers=auth_header(token.raw),
    )

    assert response.status_code == 200
    assert "shared context" in response.text
    assert token.raw not in response.text


def test_device_inbox_only_returns_allowlisted_group_tasks(client, repo):
    group = repo.create_group("codex-default")
    allowed = repo.create_device(label="allowed", group_id=group["id"])
    outsider = repo.create_device(label="outsider", group_id=group["id"])
    token = repo.create_api_token(device_id=allowed["id"], name="main")
    outsider_token = repo.create_api_token(device_id=outsider["id"], name="main")
    project = repo.create_project(
        slug="inbox-api",
        name="Inbox API",
        created_by_device_id=allowed["id"],
    )
    repo.create_task(
        project_id=project["id"],
        title="Group work",
        requested_group_id=group["id"],
    )

    allowed_response = client.get("/api/inbox", headers=auth_header(token.raw))
    outsider_response = client.get("/api/inbox", headers=auth_header(outsider_token.raw))

    assert allowed_response.status_code == 200
    assert [item["title"] for item in allowed_response.json()["tasks"]] == [
        "Group work"
    ]
    assert outsider_response.status_code == 200
    assert outsider_response.json()["tasks"] == []


def test_parallel_device_inbox_requests_use_independent_connections(
    app, repo, monkeypatch
):
    first = repo.create_device(label="parallel-device-a")
    second = repo.create_device(label="parallel-device-b")
    first_token = repo.create_api_token(device_id=first["id"], name="main")
    second_token = repo.create_api_token(device_id=second["id"], name="main")
    original = Repository.authenticate_api_token
    barrier = threading.Barrier(2)
    connection_ids: list[int] = []
    connection_ids_lock = threading.Lock()

    def synchronized_authenticate(self, raw_token):
        with connection_ids_lock:
            connection_ids.append(id(self.connection))
        barrier.wait(timeout=5)
        return original(self, raw_token)

    monkeypatch.setattr(
        Repository,
        "authenticate_api_token",
        synchronized_authenticate,
    )

    def read_inbox(raw_token: str) -> int:
        with TestClient(app) as parallel_client:
            return parallel_client.get(
                "/api/inbox",
                headers=auth_header(raw_token),
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(
            pool.map(read_inbox, (first_token.raw, second_token.raw))
        )

    assert statuses == [200, 200]
    assert len(connection_ids) == 2
    assert len(set(connection_ids)) == 2
    assert id(app.state.repo.connection) not in connection_ids


def test_device_inbox_returns_project_mentions(client, repo):
    owner = repo.create_owner_if_missing("owner", "owner-password")
    device = repo.create_device(label="codex-device-b")
    token = repo.create_api_token(device_id=device["id"], name="main")
    project = repo.create_project(
        slug="mentions-api",
        name="Mentions API",
        created_by_user_id=owner["id"],
    )
    repo.add_project_member(
        project_id=project["id"],
        device_id=device["id"],
        permission="write",
        added_by_user_id=owner["id"],
    )
    repo.create_entry(
        project_id=project["id"],
        author_user_id=owner["id"],
        source_label="owner",
        kind="message",
        body_markdown="@codex-device-b please inspect this task",
    )

    response = client.get("/api/inbox", headers=auth_header(token.raw))

    assert response.status_code == 200
    mentions = response.json()["mentions"]
    assert mentions[0]["project_slug"] == "mentions-api"
    assert mentions[0]["project_name"] == "Mentions API"
    assert "@codex-device-b" in mentions[0]["body_markdown"]
    assert mentions[0]["mention_targets"] == ["codex-device-b"]
    assert mentions[0]["matched_targets"] == ["codex-device-b"]
    assert mentions[0]["claim_status"] == "unclaimed"


def test_collaboration_mode_fans_new_user_message_to_primary_and_advisor(
    client, repo
):
    owner = repo.create_owner_if_missing("owner", "owner-password")
    primary = repo.create_device(label="codex-device-a")
    advisor = repo.create_device(label="hermes-device-a")
    read_only = repo.create_device(label="observer")
    primary_token = repo.create_api_token(primary["id"], "main")
    advisor_token = repo.create_api_token(advisor["id"], "main")
    observer_token = repo.create_api_token(read_only["id"], "main")
    project = repo.create_project(
        slug="collaboration-mode",
        name="Collaboration Mode",
        created_by_user_id=owner["id"],
    )
    for device, permission in (
        (primary, "write"),
        (advisor, "write"),
        (read_only, "read"),
    ):
        repo.add_project_member(
            project_id=project["id"],
            device_id=device["id"],
            permission=permission,
            added_by_user_id=owner["id"],
        )
    repo.set_project_primary_device(project["id"], primary["id"])
    old_entry = repo.create_entry(
        project_id=project["id"],
        author_user_id=owner["id"],
        source_label="owner",
        kind="message",
        body_markdown="This predates collaboration mode.",
        metadata_json=json.dumps({"routing": {"mode": "none", "device_ids": []}}),
    )
    repo.set_project_collaboration_mode(project["id"], True)
    user_entry = repo.create_entry(
        project_id=project["id"],
        author_user_id=owner["id"],
        source_label="owner",
        kind="message",
        body_markdown="Review this together without explicit mentions.",
        metadata_json=json.dumps({"routing": {"mode": "group", "device_ids": []}}),
    )

    primary_inbox = client.get(
        "/api/inbox", headers=auth_header(primary_token.raw)
    ).json()["mentions"]
    advisor_inbox = client.get(
        "/api/inbox", headers=auth_header(advisor_token.raw)
    ).json()["mentions"]
    observer_inbox = client.get(
        "/api/inbox", headers=auth_header(observer_token.raw)
    ).json()["mentions"]

    assert [item["id"] for item in primary_inbox] == [user_entry["id"]]
    assert [item["id"] for item in advisor_inbox] == [user_entry["id"]]
    assert observer_inbox == []
    assert old_entry["id"] not in {item["id"] for item in primary_inbox}
    assert primary_inbox[0]["delivery_reason"] == "collaboration"
    assert primary_inbox[0]["collaboration_role"] == "primary"
    assert advisor_inbox[0]["collaboration_role"] == "advisor"
    assert advisor_inbox[0]["primary_device_label"] == "codex-device-a"


def test_collaboration_mode_does_not_loop_plain_device_replies(client, repo):
    owner = repo.create_owner_if_missing("owner", "owner-password")
    primary = repo.create_device(label="codex-device-main")
    advisor = repo.create_device(label="hermes-device-review")
    primary_token = repo.create_api_token(primary["id"], "main")
    project = repo.create_project(
        slug="collaboration-no-loop",
        name="Collaboration No Loop",
        created_by_user_id=owner["id"],
    )
    for device in (primary, advisor):
        repo.add_project_member(
            project_id=project["id"],
            device_id=device["id"],
            permission="write",
            added_by_user_id=owner["id"],
        )
    repo.set_project_primary_device(project["id"], primary["id"])
    repo.set_project_collaboration_mode(project["id"], True)
    plain_reply = repo.create_entry(
        project_id=project["id"],
        author_device_id=advisor["id"],
        source_label=advisor["label"],
        kind="message",
        body_markdown="Advisory result without a target.",
    )
    targeted_reply = repo.create_entry(
        project_id=project["id"],
        author_device_id=advisor["id"],
        source_label=advisor["label"],
        kind="message",
        body_markdown="@codex-device-main please make the final decision.",
    )

    mentions = client.get(
        "/api/inbox", headers=auth_header(primary_token.raw)
    ).json()["mentions"]

    assert [item["id"] for item in mentions] == [targeted_reply["id"]]
    assert plain_reply["id"] not in {item["id"] for item in mentions}
    assert mentions[0]["delivery_reason"] == "mention"


def test_collaboration_mode_requires_primary_device(repo):
    project = repo.create_project(slug="needs-primary", name="Needs Primary")

    try:
        repo.set_project_collaboration_mode(project["id"], True)
    except ValueError as exc:
        assert "primary device" in str(exc)
    else:
        raise AssertionError("collaboration mode should require a primary device")


def test_web_group_chat_can_add_named_role_and_keep_primary_device(client, repo):
    csrf = login_owner(client)
    owner = repo.create_owner_if_missing("owner", "owner-password")
    primary = repo.create_device(label="codex-device-a")
    project = repo.create_project(
        slug="manual-group-chat",
        name="Manual Group Chat",
        created_by_user_id=owner["id"],
    )
    repo.add_project_member(
        project_id=project["id"],
        device_id=primary["id"],
        permission="write",
        added_by_user_id=owner["id"],
    )
    repo.set_project_primary_device(project["id"], primary["id"])

    response = client.post(
        "/projects/manual-group-chat/chat-roles",
        data={
            "_csrf": csrf,
            "role_name": "安全审查",
            "role_device_id": str(primary["id"]),
            "role_instructions": "检查攻击面",
        },
        follow_redirects=False,
    )
    page = client.get("/projects/manual-group-chat?view=archive")

    assert response.status_code == 303
    assert response.headers["location"].endswith(
        "/projects/manual-group-chat?view=archive#members"
    )
    assert page.status_code == 200
    assert "群聊成员" in page.text
    assert "安全审查" in page.text
    assert "由 codex-device-a 执行" in page.text
    assert repo.get_project_primary_device(project["id"])["id"] == primary["id"]


def test_mention_claim_and_handled_api_updates_inbox(client, repo):
    owner = repo.create_owner_if_missing("owner", "owner-password")
    device = repo.create_device(label="codex-device-b")
    token = repo.create_api_token(device_id=device["id"], name="main")
    project = repo.create_project(
        slug="mention-claim-api",
        name="Mention Claim API",
        created_by_user_id=owner["id"],
    )
    repo.add_project_member(
        project_id=project["id"],
        device_id=device["id"],
        permission="write",
        added_by_user_id=owner["id"],
    )
    entry = repo.create_entry(
        project_id=project["id"],
        author_user_id=owner["id"],
        source_label="owner",
        kind="message",
        body_markdown="@codex-device-b please inspect this",
    )

    initial = client.get("/api/inbox", headers=auth_header(token.raw))
    claim = client.post(
        f"/api/projects/mention-claim-api/mentions/{entry['id']}/claim",
        headers=auth_header(token.raw),
    )
    claimed_inbox = client.get("/api/inbox", headers=auth_header(token.raw))
    dispatching = client.post(
        f"/api/projects/mention-claim-api/mentions/{entry['id']}/dispatching",
        headers=auth_header(token.raw),
    )
    submitted = client.post(
        f"/api/projects/mention-claim-api/mentions/{entry['id']}/submitted",
        headers=auth_header(token.raw),
    )
    released_after_submit = client.post(
        f"/api/projects/mention-claim-api/mentions/{entry['id']}/release",
        headers=auth_header(token.raw),
    )
    handled = client.post(
        f"/api/projects/mention-claim-api/mentions/{entry['id']}/handled",
        headers=auth_header(token.raw),
    )
    handled_inbox = client.get("/api/inbox", headers=auth_header(token.raw))

    assert initial.status_code == 200
    assert initial.json()["mentions"][0]["claim_status"] == "unclaimed"
    assert claim.status_code == 200
    assert claim.json()["receipt"]["status"] == "claimed"
    assert claimed_inbox.json()["mentions"][0]["claim_status"] == "claimed"
    assert dispatching.status_code == 200
    assert dispatching.json()["receipt"]["status"] == "dispatching"
    assert submitted.status_code == 200
    assert submitted.json()["receipt"]["status"] == "submitted"
    assert released_after_submit.json()["receipt"]["status"] == "submitted"
    assert submitted.json()["receipt"]["delivery_id"] == claim.json()["receipt"]["delivery_id"]
    assert handled.status_code == 200
    assert handled.json()["receipt"]["status"] == "handled"
    assert handled_inbox.json()["mentions"] == []


def test_read_only_target_can_claim_mention_without_writing_entries(client, repo):
    owner = repo.create_owner_if_missing("owner", "owner-password")
    device = repo.create_device(label="codex-device-read")
    token = repo.create_api_token(device_id=device["id"], name="main")
    project = repo.create_project(
        slug="mention-read-api",
        name="Mention Read API",
        created_by_user_id=owner["id"],
    )
    repo.add_project_member(
        project_id=project["id"],
        device_id=device["id"],
        permission="read",
        added_by_user_id=owner["id"],
    )
    entry = repo.create_entry(
        project_id=project["id"],
        author_user_id=owner["id"],
        source_label="owner",
        kind="message",
        body_markdown="@codex-device-read please read this",
    )

    claim = client.post(
        f"/api/projects/mention-read-api/mentions/{entry['id']}/claim",
        headers=auth_header(token.raw),
    )
    handled = client.post(
        f"/api/projects/mention-read-api/mentions/{entry['id']}/handled",
        headers=auth_header(token.raw),
    )
    write_response = client.post(
        "/api/projects/mention-read-api/entries",
        headers=auth_header(token.raw),
        json={"kind": "message", "body_markdown": "read-only should not write"},
    )

    assert claim.status_code == 200
    assert handled.status_code == 200
    assert write_response.status_code == 403


def test_task_prompt_api_requires_project_membership(client, repo):
    allowed = repo.create_device(label="allowed")
    blocked = repo.create_device(label="blocked")
    allowed_token = repo.create_api_token(device_id=allowed["id"], name="main")
    blocked_token = repo.create_api_token(device_id=blocked["id"], name="main")
    project = repo.create_project(
        slug="prompt-api",
        name="Prompt API",
        created_by_device_id=allowed["id"],
    )
    task = repo.create_task(project_id=project["id"], title="Promptable")

    allowed_response = client.get(
        f"/api/projects/prompt-api/tasks/{task['id']}/prompt.md",
        headers=auth_header(allowed_token.raw),
    )
    blocked_response = client.get(
        f"/api/projects/prompt-api/tasks/{task['id']}/prompt.md",
        headers=auth_header(blocked_token.raw),
    )

    assert allowed_response.status_code == 200
    assert "Promptable" in allowed_response.text
    assert blocked_response.status_code == 403


def test_primary_device_label_flows_to_inbox_and_task_prompt(client, repo):
    owner = repo.create_owner_if_missing("owner", "owner-password")
    primary = repo.create_device(label="codex-device-a")
    helper = repo.create_device(label="hermes-device-a")
    helper_token = repo.create_api_token(device_id=helper["id"], name="main")
    project = repo.create_project(
        slug="primary-routing",
        name="Primary Routing",
        created_by_user_id=owner["id"],
    )
    repo.add_project_member(
        project_id=project["id"],
        device_id=primary["id"],
        permission="write",
        added_by_user_id=owner["id"],
    )
    repo.add_project_member(
        project_id=project["id"],
        device_id=helper["id"],
        permission="write",
        added_by_user_id=owner["id"],
    )
    repo.set_project_primary_device(project["id"], primary["id"])
    repo.create_entry(
        project_id=project["id"],
        author_user_id=owner["id"],
        source_label="owner",
        kind="message",
        body_markdown="@hermes-device-a review this as an assistant",
    )
    task = repo.create_task(
        project_id=project["id"],
        title="Assistant review",
        assigned_device_id=helper["id"],
    )

    inbox = client.get("/api/inbox", headers=auth_header(helper_token.raw))
    prompt = client.get(
        f"/api/projects/primary-routing/tasks/{task['id']}/prompt.md",
        headers=auth_header(helper_token.raw),
    )

    assert inbox.status_code == 200
    assert inbox.json()["mentions"][0]["primary_device_label"] == ""
    assert inbox.json()["mentions"][0]["project_primary_device_id"] == primary["id"]
    assert prompt.status_code == 200
    assert "Primary device: @codex-device-a" in prompt.text
    assert "Handle this task directly" in prompt.text
    assert "include @codex-device-a in your Hub reply" not in prompt.text


def test_project_memory_handoff_and_diff_api_supports_primary_governance(
    client, repo
):
    owner = repo.create_owner_if_missing("owner", "owner-password")
    primary = repo.create_device(label="memory-primary")
    secondary = repo.create_device(label="memory-secondary")
    primary_token = repo.create_api_token(primary["id"], "main")
    secondary_token = repo.create_api_token(secondary["id"], "main")
    project = repo.create_project(
        slug="memory-api",
        name="Memory API",
        created_by_user_id=owner["id"],
    )
    repo.add_project_member(project["id"], primary["id"], "admin")
    repo.add_project_member(project["id"], secondary["id"], "write")
    repo.set_project_primary_device(project["id"], primary["id"])

    blocked_memory = client.put(
        "/api/projects/memory-api/memory",
        headers=auth_header(secondary_token.raw),
        json={"goal": "Secondary must not publish canonical memory"},
    )
    memory = client.put(
        "/api/projects/memory-api/memory",
        headers=auth_header(primary_token.raw),
        json={
            "goal": "Seamless multi-device continuation",
            "architecture": "Hub canonical memory plus local Markdown mirror",
            "user_preferences": "Create a new local Codex conversation per item",
        },
    )
    handoff = client.post(
        "/api/projects/memory-api/handoffs",
        headers=auth_header(secondary_token.raw),
        json={
            "title": "Secondary findings",
            "summary_markdown": "One project record is missing on the secondary.",
            "target_device_id": primary["id"],
            "manifest": {
                "files": [
                    {
                        "path": "PROJECT_MEMORY.md",
                        "sha256": "b" * 64,
                        "size": 8,
                    }
                ]
            },
        },
    )
    primary_snapshot = client.post(
        "/api/projects/memory-api/sync-snapshots",
        headers=auth_header(primary_token.raw),
        json={
            "memory_version": 1,
            "git_commit": "primary-head",
            "manifest": {
                "files": [
                    {
                        "path": "PROJECT_MEMORY.md",
                        "sha256": "a" * 64,
                        "size": 10,
                    },
                    {
                        "path": "records/new.md",
                        "sha256": "c" * 64,
                        "size": 5,
                    },
                ]
            },
        },
    )
    secondary_snapshot = client.post(
        "/api/projects/memory-api/sync-snapshots",
        headers=auth_header(secondary_token.raw),
        json={
            "memory_version": 0,
            "git_commit": "secondary-head",
            "manifest": {
                "files": [
                    {
                        "path": "PROJECT_MEMORY.md",
                        "sha256": "b" * 64,
                        "size": 8,
                    }
                ]
            },
        },
    )
    diff = client.get(
        "/api/projects/memory-api/sync-diff",
        headers=auth_header(secondary_token.raw),
    )
    accepted = client.post(
        f"/api/projects/memory-api/handoffs/{handoff.json()['handoff']['id']}/accept",
        headers=auth_header(primary_token.raw),
    )
    memory_markdown = client.get(
        "/api/projects/memory-api/memory.md",
        headers=auth_header(secondary_token.raw),
    )
    runtime_memory_markdown = client.get(
        "/api/projects/memory-api/memory/runtime.md",
        headers=auth_header(secondary_token.raw),
    )

    assert blocked_memory.status_code == 403
    assert memory.status_code == 200
    assert memory.json()["memory"]["version"] == 1
    assert handoff.status_code == 201
    assert primary_snapshot.status_code == 201
    assert secondary_snapshot.status_code == 201
    assert diff.status_code == 200
    assert [
        item["path"]
        for item in diff.json()["comparison"]["missing_on_target"]
    ] == ["records/new.md"]
    assert accepted.status_code == 200
    assert accepted.json()["handoff"]["status"] == "accepted"
    assert memory_markdown.status_code == 200
    assert "Seamless multi-device continuation" in memory_markdown.text
    assert runtime_memory_markdown.status_code == 200
    assert "Seamless multi-device continuation" in runtime_memory_markdown.text
    assert "Recent Handoffs" not in runtime_memory_markdown.text
    assert len(runtime_memory_markdown.text) < len(memory_markdown.text)


def test_project_memory_rejects_question_mark_encoding_loss(client, repo):
    primary = repo.create_device(label="memory-utf8-primary")
    token = repo.create_api_token(primary["id"], "main")
    project = repo.create_project(
        slug="memory-utf8",
        name="Memory UTF-8",
        created_by_device_id=primary["id"],
    )
    repo.set_project_primary_device(project["id"], primary["id"])
    damaged = (
        "???????????????????? Codex Hub\n"
        "???????????????????? Project Memory"
    )

    response = client.put(
        "/api/projects/memory-utf8/memory",
        headers=auth_header(token.raw),
        json={"goal": damaged},
    )
    too_large = client.put(
        "/api/projects/memory-utf8/memory",
        headers=auth_header(token.raw),
        json={"goal": "x" * 12_001},
    )

    assert response.status_code == 422
    assert "Project memory field 'goal'" in response.json()["detail"]
    assert too_large.status_code == 422
    assert "too large" in too_large.json()["detail"]
    assert repo.get_latest_project_control(project["id"]) is None


def test_projects_page_requires_login(client):
    response = client.get("/projects", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].endswith("/login")


def test_healthz_is_public(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "service": "codex-hub"}


def test_speech_capabilities_use_direct_client_voice_without_hub_audio(client):
    response = client.get("/api/speech/capabilities")

    assert response.status_code == 200
    assert response.json() == {
        "contract_version": "voice-v1",
        "status": "client_available",
        "transport": "direct_on_device",
        "tts": {
            "enabled": True,
            "streaming": False,
            "server_synthesis": False,
            "client_modes": ["android_native", "web_speech"],
            "output_formats": ["audio/mpeg", "audio/ogg", "audio/wav"],
        },
        "voice_input": {
            "enabled": True,
            "client_modes": ["android_native", "web_speech"],
            "sends_audio_to_hub": False,
            "send_requires_confirmation": True,
        },
    }


def test_mobile_notifications_require_login_and_return_new_device_replies(client, repo):
    unauthenticated = client.get("/api/mobile/notifications")
    owner = repo.get_user_by_username("owner")
    device = repo.create_device(label="notify-device")
    project = repo.create_project(
        slug="mobile-notify",
        name="Mobile Notify",
        created_by_user_id=owner["id"],
    )
    user_entry = repo.create_entry(
        project_id=project["id"],
        author_user_id=owner["id"],
        source_label="owner",
        kind="message",
        body_markdown="user text should not notify",
    )
    device_entry = repo.create_entry(
        project_id=project["id"],
        author_device_id=device["id"],
        source_label="notify-device",
        kind="message",
        body_markdown="任务已经完成，可以查看结果。",
    )
    login_owner(client)

    response = client.get(
        "/api/mobile/notifications",
        params={"after_entry_id": user_entry["id"]},
    )

    assert unauthenticated.status_code == 401
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "latest_entry_id": device_entry["id"],
        "notifications": [
            {
                "entry_id": device_entry["id"],
                "project_slug": "mobile-notify",
                "project_name": "Mobile Notify",
                "source_label": "notify-device",
                "kind": "message",
                "body": "任务已经完成，可以查看结果。",
                "created_at": device_entry["created_at"],
                "task_id": None,
            }
        ],
    }


def test_owner_credentials_follow_configuration(client, repo):
    repo.create_owner_if_missing("owner", "old-password")

    repo.create_owner_if_missing("BYBC", "new-password")

    assert repo.authenticate_user("BYBC", "new-password") is not None
    assert repo.authenticate_user("owner", "old-password") is None


def test_login_project_creation_and_entry_flow(client):
    csrf = login_owner(client)
    project = client.post(
        "/projects",
        data={
            "slug": "web-plan",
            "name": "Web Plan",
            "description": "UI flow",
            "_csrf": csrf,
        },
        follow_redirects=False,
    )
    entry = client.post(
        "/projects/web-plan/entries",
        data={"kind": "markdown", "body_markdown": "Owner context", "_csrf": csrf},
        follow_redirects=False,
    )
    detail = client.get("/projects/web-plan?view=archive")

    assert project.status_code == 303
    assert entry.status_code == 303
    assert "Owner context" in detail.text


def test_web_chat_uploads_and_renders_safe_inline_images(client, repo):
    csrf = login_owner(client)
    client.post(
        "/projects",
        data={
            "slug": "image-chat",
            "name": "Image Chat",
            "description": "Uploads",
            "_csrf": csrf,
        },
    )
    upload = client.post(
        "/projects/image-chat/attachments",
        data={"_csrf": csrf},
        files={"file": ("示意图.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        follow_redirects=False,
    )
    project = repo.get_project_by_slug("image-chat")
    entry = repo.list_project_entries(project["id"])[0]
    attachment = repo.list_attachments(project["id"])[0]
    detail = client.get("/projects/image-chat?view=archive")
    preview = client.get(
        f"/projects/image-chat/attachments/{attachment['id']}?inline=1"
    )

    assert upload.status_code == 303
    assert attachment["entry_id"] == entry["id"]
    assert 'class="chat-message from-user"' in detail.text
    assert 'data-attachment-button' in detail.text
    assert 'data-speech-contract="voice-v1"' in detail.text
    assert 'data-voice-session' in detail.text
    assert 'data-voice-input' not in detail.text
    assert 'data-voice-speak-latest' not in detail.text
    assert 'class="message-image"' in detail.text
    assert f"attachments/{attachment['id']}?inline=1" in detail.text
    assert "示意图.png" in detail.text
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/png"
    assert preview.headers["content-disposition"].startswith("inline;")
    assert preview.headers["x-content-type-options"] == "nosniff"


def test_web_composer_sends_message_and_multiple_files_as_one_entry(client, repo):
    csrf = login_owner(client)
    client.post(
        "/projects",
        data={
            "slug": "composer-files",
            "name": "Composer Files",
            "description": "Staged attachments",
            "_csrf": csrf,
        },
    )

    response = client.post(
        "/projects/composer-files/entries",
        data={
            "kind": "message",
            "body_markdown": "照片和说明一起发送",
            "_csrf": csrf,
        },
        files=[
            ("files", ("现场图.png", b"\x89PNG\r\n\x1a\n", "image/png")),
            ("files", ("需求.txt", b"attachment details", "text/plain")),
        ],
        headers={"X-Requested-With": "codex-hub-composer"},
        follow_redirects=False,
    )

    project = repo.get_project_by_slug("composer-files")
    entries = repo.list_project_entries(project["id"])
    attachments = repo.list_attachments(project["id"])

    assert response.status_code == 201
    assert response.json()["entry"]["body_markdown"] == "照片和说明一起发送"
    assert len(response.json()["attachments"]) == 2
    assert 'class="chat-message from-user"' in response.json()["entry_html"]
    assert "照片和说明一起发送" in response.json()["entry_html"]
    assert "现场图.png" in response.json()["entry_html"]
    assert "需求.txt" in response.json()["entry_html"]
    assert len(entries) == 1
    assert entries[0]["kind"] == "message"
    assert {item["entry_id"] for item in attachments} == {entries[0]["id"]}
    assert {item["original_filename"] for item in attachments} == {
        "现场图.png",
        "需求.txt",
    }


def test_image_thumbnails_preserve_dimensions_and_message_media_order(client, repo):
    csrf = login_owner(client)
    client.post(
        "/projects",
        data={
            "slug": "reading-media",
            "name": "Reading Media",
            "description": "Reading first",
            "_csrf": csrf,
        },
    )
    image_buffer = BytesIO()
    Image.new("RGB", (120, 60), color=(230, 180, 40)).save(image_buffer, "PNG")
    upload = client.post(
        "/projects/reading-media/attachments",
        data={"_csrf": csrf},
        files={"file": ("wide.png", image_buffer.getvalue(), "image/png")},
        follow_redirects=False,
    )
    project = repo.get_project_by_slug("reading-media")
    attachment = repo.list_attachments(project["id"])[0]
    detail = client.get("/projects/reading-media?view=archive")
    thumbnail = client.get(
        f"/projects/reading-media/attachments/{attachment['id']}?inline=1&size=thumb"
    )

    assert upload.status_code == 303
    assert thumbnail.status_code == 200
    assert thumbnail.headers["content-type"] == "image/webp"
    with Image.open(BytesIO(thumbnail.content)) as rendered:
        assert rendered.size == (120, 60)
    assert 'style="aspect-ratio: 120 / 60"' in detail.text
    assert detail.text.index("data-markdown-source") < detail.text.index(
        "message-image-grid"
    )
    assert 'data-media-category="image"' in detail.text
    assert "Office / PDF 使用下载后外部工具打开" in detail.text


def test_video_is_lazy_and_pdf_remains_a_download(client):
    csrf = login_owner(client)
    client.post(
        "/projects",
        data={
            "slug": "lazy-media",
            "name": "Lazy Media",
            "description": "No autoplay",
            "_csrf": csrf,
        },
    )
    client.post(
        "/projects/lazy-media/attachments",
        data={"_csrf": csrf},
        files={"file": ("clip.mp4", b"not-loaded-until-click", "video/mp4")},
    )
    client.post(
        "/projects/lazy-media/attachments",
        data={"_csrf": csrf},
        files={"file": ("brief.pdf", b"%PDF-1.4", "application/pdf")},
    )
    detail = client.get("/projects/lazy-media?view=archive")

    assert 'data-video-attachment' in detail.text
    assert "点击后才加载视频" in detail.text
    assert "<video" not in detail.text
    pdf_item = detail.text[detail.text.index("brief.pdf") - 500 :]
    assert "data-video-attachment" not in pdf_item[:500]
    assert 'data-media-type="document"' in detail.text


def test_web_project_page_supports_control_tasks_and_task_chat(client):
    csrf = login_owner(client)
    client.post(
        "/projects",
        data={
            "slug": "chat-work",
            "name": "Chat Work",
            "description": "Coordination",
            "_csrf": csrf,
        },
    )
    control = client.post(
        "/projects/chat-work/control",
        data={
            "goal": "Coordinate devices",
            "constraints_text": "Use allowlist",
            "_csrf": csrf,
        },
        follow_redirects=False,
    )
    task = client.post(
        "/projects/chat-work/tasks",
        data={
            "title": "Device B review",
            "description": "Read prompt and respond",
            "priority": "high",
            "_csrf": csrf,
        },
        follow_redirects=False,
    )
    detail = client.get("/projects/chat-work?view=archive")

    assert control.status_code == 303
    assert task.status_code == 303
    assert "Coordinate devices" in detail.text
    assert "Device B review" in detail.text
    assert "Waiting for me" not in detail.text
    assert "长期项目记忆" in detail.text
    assert "复制 Codex 提示" in detail.text
    assert "项目对话 · 每条消息可选择指定成员或全组协作" in detail.text


def test_web_project_memory_drawer_handoff_and_primary_switch_reminder(client, repo):
    csrf = login_owner(client)
    owner = repo.get_user_by_username("owner")
    device_a = repo.create_device(label="web-memory-a")
    device_b = repo.create_device(label="web-memory-b")
    project = repo.create_project(
        slug="web-memory",
        name="Web Memory",
        created_by_user_id=owner["id"],
    )
    repo.add_project_member(project["id"], device_a["id"], "admin")
    repo.add_project_member(project["id"], device_b["id"], "write")
    repo.set_project_primary_device(project["id"], device_a["id"])
    repo.save_project_control(
        project["id"],
        goal="Keep durable context",
        architecture="Codex-style shell",
        user_preferences="New local conversation per request",
    )

    handoff = client.post(
        "/projects/web-memory/handoffs",
        data={
            "_csrf": csrf,
            "title": "Before primary switch",
            "summary_markdown": "All durable decisions are recorded.",
            "source_device_id": str(device_a["id"]),
            "target_device_id": str(device_b["id"]),
        },
        follow_redirects=False,
    )
    switched = client.post(
        "/projects/web-memory/primary-device",
        data={"_csrf": csrf, "primary_device_id": str(device_b["id"])},
        follow_redirects=False,
    )
    page = client.get("/projects/web-memory?view=archive")
    entries = repo.list_project_entries(project["id"])

    assert handoff.status_code == 303
    assert handoff.headers["location"].endswith(
        "/projects/web-memory?view=archive#project-memory"
    )
    assert switched.status_code == 303
    assert 'data-memory-drawer' in page.text
    assert "Keep durable context" in page.text
    assert "Before primary switch" in page.text
    assert "All durable decisions are recorded." in page.text
    assert "主设备已从 `web-memory-a` 切换为 `web-memory-b`" in entries[-1][
        "body_markdown"
    ]


def test_web_codex_workspace_conversation_creates_targeted_task(client, repo):
    csrf = login_owner(client)
    owner = repo.get_user_by_username("owner")
    group = repo.create_group("codex-default")
    device = repo.create_device(label="codex-device-a", group_id=group["id"])
    project = repo.create_project(
        slug="codex-workspaces",
        name="Codex Workspaces",
        created_by_user_id=owner["id"],
    )
    repo.add_project_member(
        project_id=project["id"],
        device_id=device["id"],
        permission="write",
        added_by_user_id=owner["id"],
    )

    create_workspace = client.post(
        "/projects/codex-workspaces/codex-workspaces",
        data={
            "workspace_slug": "zbcs",
            "name": "zbcs",
            "description": "school project",
            "_csrf": csrf,
        },
        follow_redirects=False,
    )
    detail = client.get("/projects/codex-workspaces?view=archive")
    workspace = repo.get_codex_workspace_by_slug("zbcs")
    create_conversation = client.post(
        f"/projects/codex-workspaces/codex-workspaces/{workspace['id']}/conversations",
        data={
            "title": "检查 skill",
            "initial_prompt": "检查现有的 skill",
            "target_device_id": str(device["id"]),
            "_csrf": csrf,
        },
        follow_redirects=False,
    )
    tasks = repo.list_tasks(project["id"])
    prompt = repo.build_task_prompt_markdown("codex-workspaces", tasks[0]["id"])

    assert create_workspace.status_code == 303
    assert "Codex 项目" in detail.text
    assert "zbcs" in detail.text
    assert create_conversation.status_code == 303
    assert tasks[0]["assigned_device_id"] == device["id"]
    assert "Codex target project slug: zbcs" in prompt
    assert "检查现有的 skill" in prompt


def test_project_page_marks_recent_api_device_online(client, repo):
    csrf = login_owner(client)
    device = repo.create_device(label="Device Online")
    token = repo.create_api_token(device_id=device["id"], name="main")
    project = repo.create_project(
        slug="presence-web",
        name="Presence Web",
        created_by_user_id=repo.get_user_by_username("owner")["id"],
    )
    repo.add_project_member(
        project_id=project["id"],
        device_id=device["id"],
        permission="write",
        added_by_user_id=repo.get_user_by_username("owner")["id"],
    )

    client.get("/api/inbox", headers=auth_header(token.raw))
    detail = client.get("/projects/presence-web?view=archive")

    assert csrf
    assert "Device Online" in detail.text
    assert 'data-presence="agent"' in detail.text
    assert 'data-agent-presence="online"' in detail.text
    assert "Codex已接入" in detail.text


def test_project_page_marks_recent_hermes_device_as_hermes_connected(client, repo):
    login_owner(client)
    owner = repo.get_user_by_username("owner")
    device = repo.create_device(label="hermes-device-a")
    token = repo.create_api_token(device_id=device["id"], name="main")
    project = repo.create_project(
        slug="presence-hermes",
        name="Presence Hermes",
        created_by_user_id=owner["id"],
    )
    repo.add_project_member(
        project_id=project["id"],
        device_id=device["id"],
        permission="write",
        added_by_user_id=owner["id"],
    )

    client.get("/api/inbox", headers=auth_header(token.raw))
    detail = client.get("/projects/presence-hermes?view=archive")

    assert "hermes-device-a" in detail.text
    assert 'data-presence="agent"' in detail.text
    assert "Hermes已接入" in detail.text
    assert "hermes-device-a</strong>\n              <small>write · Codex已接入" not in detail.text


def test_web_device_heartbeat_marks_project_member_online(client, repo):
    csrf = login_owner(client)
    owner = repo.get_user_by_username("owner")
    device = repo.create_device(label="Browser Device")
    project = repo.create_project(
        slug="browser-presence",
        name="Browser Presence",
        created_by_user_id=owner["id"],
    )
    repo.add_project_member(
        project_id=project["id"],
        device_id=device["id"],
        permission="write",
        added_by_user_id=owner["id"],
    )

    before = client.get("/projects/browser-presence?view=archive")
    heartbeat = client.post(
        f"/devices/{device['id']}/heartbeat",
        data={"_csrf": csrf},
        follow_redirects=False,
    )
    after = client.get("/projects/browser-presence?view=archive")

    assert 'data-presence="offline"' in before.text
    assert heartbeat.status_code == 200
    assert heartbeat.json()["ok"] is True
    assert 'data-presence="web"' in after.text
    assert 'data-agent-presence="offline"' in after.text
    assert "网页登录在线，Codex未接入" in after.text


def test_project_composer_renders_mention_picker(client, repo):
    login_owner(client)
    owner = repo.get_user_by_username("owner")
    group = repo.create_group("codex-default")
    device = repo.create_device(label="codex-device-b", group_id=group["id"])
    project = repo.create_project(
        slug="mention-picker",
        name="Mention Picker",
        created_by_user_id=owner["id"],
    )
    repo.add_project_member(
        project_id=project["id"],
        device_id=device["id"],
        permission="write",
        added_by_user_id=owner["id"],
    )

    detail = client.get("/projects/mention-picker?view=archive")

    assert "data-mention-composer" in detail.text
    assert "data-mention-menu" in detail.text
    assert 'data-mention-value="@codex-device-b"' in detail.text
    assert 'data-mention-value="@codex-default"' in detail.text
    assert 'name="delivery_mode" data-delivery-mode' in detail.text
    assert 'name="target_device_ids"' in detail.text
    assert 'data-delivery-member' in detail.text
    assert 'value="selected" selected' in detail.text
    assert 'data-auto-refresh="chat"' in detail.text
    assert 'data-auto-refresh="tasks"' in detail.text
    assert "data-web-presence-device" in detail.text
    assert "/heartbeat" in detail.text
    assert "codex-hub-refresh" in detail.text


def test_project_page_renders_codex_connect_button(client, repo):
    login_owner(client)
    owner = repo.get_user_by_username("owner")
    device = repo.create_device(label="codex-device-a")
    project = repo.create_project(
        slug="connect-panel",
        name="Connect Panel",
        created_by_user_id=owner["id"],
    )
    repo.add_project_member(
        project_id=project["id"],
        device_id=device["id"],
        permission="write",
        added_by_user_id=owner["id"],
    )

    detail = client.get("/projects/connect-panel?view=archive")

    assert "data-connect-toggle" in detail.text
    assert "data-connect-panel" in detail.text
    assert "data-connect-command" in detail.text
    assert "data-copy-connect-command" in detail.text
    assert "codex-device-agent.ps1" in detail.text
    assert "CODEX_HUB_TOKEN" in detail.text
    assert "ProjectMapPath" in detail.text
    assert "codex-project-map.json" in detail.text
    assert "-OpenCodex" in detail.text
    assert "execCommand" in detail.text
    assert "Codex" in detail.text


def test_web_forms_reject_missing_csrf(client):
    login_owner(client)

    response = client.post(
        "/projects",
        data={"slug": "no-csrf", "name": "No CSRF", "description": ""},
        follow_redirects=False,
    )

    assert response.status_code == 403


def test_web_forms_reject_csrf_cookie_not_bound_to_session(client, repo):
    login_owner(client)
    client.cookies.set("codex_hub_csrf", "forged-csrf")

    response = client.post(
        "/projects",
        data={
            "slug": "forged-csrf",
            "name": "Forged CSRF",
            "description": "",
            "_csrf": "forged-csrf",
        },
    )

    assert response.status_code == 403
    assert repo.get_project_by_slug("forged-csrf") is None


def test_authenticated_page_rotates_a_missing_csrf_cookie(client, repo):
    old_csrf = login_owner(client)
    client.cookies.delete("codex_hub_csrf")

    page = client.get("/projects?list=1")
    new_csrf = client.cookies.get("codex_hub_csrf")
    created = client.post(
        "/projects",
        data={
            "slug": "rotated-csrf",
            "name": "Rotated CSRF",
            "description": "",
            "_csrf": new_csrf,
        },
        follow_redirects=False,
    )

    assert page.status_code == 200
    assert new_csrf and new_csrf != old_csrf
    assert created.status_code == 303
    assert repo.get_project_by_slug("rotated-csrf") is not None


def test_missing_device_page_returns_404_instead_of_500(client):
    login_owner(client)

    response = client.get("/devices/999999/inbox")

    assert response.status_code == 404
    assert response.json()["detail"] == "device not found"


def test_admin_form_rejects_non_numeric_group_id(client):
    csrf = login_owner(client)

    response = client.post(
        "/devices",
        data={
            "label": "invalid-group-device",
            "group_id": "not-a-number",
            "note": "",
            "_csrf": csrf,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "value must be an integer"


def test_admin_forms_reject_missing_group_device_and_membership_targets(client, repo):
    csrf = login_owner(client)
    project = repo.create_project(
        slug="missing-targets",
        name="Missing Targets",
        created_by_user_id=repo.get_user_by_username("owner")["id"],
    )

    missing_group = client.post(
        "/devices",
        data={
            "label": "missing-group-device",
            "group_id": "999999",
            "note": "",
            "_csrf": csrf,
        },
    )
    missing_token_device = client.post(
        "/admin/tokens",
        data={"device_id": "999999", "name": "invalid", "_csrf": csrf},
    )
    missing_member_device = client.post(
        f"/projects/{project['slug']}/members",
        data={"device_id": "999999", "permission": "read", "_csrf": csrf},
    )

    assert missing_group.status_code == 422
    assert missing_group.json()["detail"] == "device group not found"
    assert missing_token_device.status_code == 404
    assert missing_token_device.json()["detail"] == "device not found"
    assert missing_member_device.status_code == 404
    assert missing_member_device.json()["detail"] == "device not found"


def test_web_message_fallback_redirects_without_a_scroll_fragment(client, repo):
    project = repo.create_project(
        slug="mobile-chat",
        name="Mobile Chat",
        created_by_user_id=repo.get_user_by_username("owner")["id"],
    )
    csrf = login_owner(client)

    response = client.post(
        "/projects/mobile-chat/entries",
        data={
            "_csrf": csrf,
            "kind": "message",
            "body_markdown": "keep the mobile chat at the latest message",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/projects/mobile-chat?view=archive"
    assert repo.list_project_entries(project["id"])[-1]["body_markdown"] == (
        "keep the mobile chat at the latest message"
    )


def test_web_message_composer_returns_json_without_reloading_the_page(client, repo):
    project = repo.create_project(
        slug="async-mobile-chat",
        name="Async Mobile Chat",
        created_by_user_id=repo.get_user_by_username("owner")["id"],
    )
    csrf = login_owner(client)

    response = client.post(
        "/projects/async-mobile-chat/entries",
        data={
            "_csrf": csrf,
            "kind": "message",
            "body_markdown": "send without a document navigation",
        },
        headers={"X-Requested-With": "codex-hub-composer"},
        follow_redirects=False,
    )

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["ok"] is True
    assert response.json()["entry"]["project_id"] == project["id"]
    assert response.json()["entry"]["body_markdown"] == (
        "send without a document navigation"
    )
    assert 'data-entry-id="' in response.json()["entry_html"]


def test_web_chat_updates_return_only_new_rendered_entries(client, repo):
    project = repo.create_project(
        slug="incremental-mobile-chat",
        name="Incremental Mobile Chat",
        created_by_user_id=repo.get_user_by_username("owner")["id"],
    )
    first = repo.create_entry(
        project_id=project["id"],
        author_user_id=repo.get_user_by_username("owner")["id"],
        source_label="owner",
        kind="message",
        body_markdown="already rendered",
    )
    device = repo.create_device(label="incremental-device")
    reply = repo.create_entry(
        project_id=project["id"],
        author_device_id=device["id"],
        source_label="incremental-device",
        kind="message",
        body_markdown="small live update",
    )
    login_owner(client)

    response = client.get(
        "/projects/incremental-mobile-chat/chat-updates",
        params={"after_entry_id": first["id"]},
    )
    detail = client.get("/projects/incremental-mobile-chat?view=archive")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["latest_entry_id"] == reply["id"]
    assert response.json()["next_entry_id"] == reply["id"]
    assert response.json()["has_more"] is False
    assert [item["id"] for item in response.json()["entries"]] == [reply["id"]]
    assert "small live update" in response.json()["entries"][0]["html"]
    assert 'class="chat-message from-device"' in response.json()["entries"][0]["html"]
    assert "data-chat-updates-url=" in detail.text


def test_web_chat_updates_request_a_reset_after_cursor_rollback(client, repo):
    project = repo.create_project(
        slug="rollback-chat-cursor",
        name="Rollback Chat Cursor",
        created_by_user_id=repo.get_user_by_username("owner")["id"],
    )
    entry = repo.create_entry(
        project_id=project["id"],
        author_user_id=repo.get_user_by_username("owner")["id"],
        source_label="owner",
        kind="message",
        body_markdown="current entry",
    )
    login_owner(client)

    response = client.get(
        "/projects/rollback-chat-cursor/chat-updates",
        params={"after_entry_id": entry["id"] + 100},
    )

    assert response.status_code == 200
    assert response.json()["entries"] == []
    assert response.json()["reset_required"] is True


def test_project_and_task_surfaces_are_isolated_in_both_directions(client, repo):
    device = repo.create_device(label="scope-device")
    token = repo.create_api_token(device_id=device["id"], name="scope")
    project = repo.create_project(
        slug="scope-boundary",
        name="Scope Boundary",
        created_by_device_id=device["id"],
    )
    first_task = repo.create_task(project_id=project["id"], title="First task")
    second_task = repo.create_task(project_id=project["id"], title="Second task")
    first_project_entry = repo.create_entry(
        project_id=project["id"],
        author_device_id=device["id"],
        source_label="scope-device",
        kind="message",
        body_markdown="project-visible-one",
    )
    first_task_entry = repo.create_entry(
        project_id=project["id"],
        task_id=first_task["id"],
        author_device_id=device["id"],
        source_label="scope-device",
        kind="message",
        body_markdown="first-task-private-message",
    )
    second_task_entry = repo.create_entry(
        project_id=project["id"],
        task_id=second_task["id"],
        author_device_id=device["id"],
        source_label="scope-device",
        kind="message",
        body_markdown="second-task-private-message",
    )
    second_project_entry = repo.create_entry(
        project_id=project["id"],
        author_device_id=device["id"],
        source_label="scope-device",
        kind="message",
        body_markdown="project-visible-two",
    )
    for entry, filename in (
        (first_project_entry, "project-visible.txt"),
        (first_task_entry, "first-task-private.txt"),
        (second_task_entry, "second-task-private.txt"),
    ):
        repo.create_attachment(
            project_id=project["id"],
            entry_id=entry["id"],
            original_filename=filename,
            stored_relpath=f"scope/{filename}",
            content_type="text/plain",
            byte_size=12,
            sha256="b" * 64,
            created_by_device_id=device["id"],
        )

    project_api = client.get(
        "/api/projects/scope-boundary/entries",
        headers=auth_header(token.raw),
    )
    first_task_api = client.get(
        f"/api/projects/scope-boundary/tasks/{first_task['id']}",
        headers=auth_header(token.raw),
    )
    first_task_prompt = client.get(
        f"/api/projects/scope-boundary/tasks/{first_task['id']}/prompt.md",
        headers=auth_header(token.raw),
    )
    login_owner(client)
    project_page = client.get("/projects/scope-boundary?view=archive")
    first_task_page = client.get(
        f"/projects/scope-boundary/tasks/{first_task['id']}"
    )
    live_updates = client.get(
        "/projects/scope-boundary/chat-updates",
        params={"after_entry_id": first_project_entry["id"]},
    )

    assert [item["id"] for item in project_api.json()["entries"]] == [
        first_project_entry["id"],
        second_project_entry["id"],
    ]
    assert [item["original_filename"] for item in project_api.json()["attachments"]] == [
        "project-visible.txt"
    ]
    assert [item["id"] for item in first_task_api.json()["entries"]] == [
        first_task_entry["id"]
    ]
    assert "first-task-private-message" in first_task_prompt.text
    assert "first-task-private.txt" in first_task_prompt.text
    assert "project-visible-one" not in first_task_prompt.text
    assert "project-visible.txt" not in first_task_prompt.text
    assert "second-task-private-message" not in first_task_prompt.text
    assert "second-task-private.txt" not in first_task_prompt.text
    assert "project-visible-one" in project_page.text
    assert "project-visible-two" in project_page.text
    assert "first-task-private-message" not in project_page.text
    assert "second-task-private-message" not in project_page.text
    assert "first-task-private-message" in first_task_page.text
    assert "project-visible-one" not in first_task_page.text
    assert "second-task-private-message" not in first_task_page.text
    assert [item["id"] for item in live_updates.json()["entries"]] == [
        second_project_entry["id"]
    ]


def test_versioned_static_assets_are_immutable_and_large_pages_use_gzip(client, repo):
    project = repo.create_project(
        slug="compressed-chat",
        name="Compressed Chat",
        created_by_user_id=repo.get_user_by_username("owner")["id"],
    )
    for index in range(20):
        repo.create_entry(
            project_id=project["id"],
            author_user_id=repo.get_user_by_username("owner")["id"],
            source_label="owner",
            kind="message",
            body_markdown=f"compressible message {index} " * 10,
        )
    login_owner(client)

    detail = client.get(
        "/projects/compressed-chat?view=archive", headers={"Accept-Encoding": "gzip"}
    )
    static = client.get(asset_url(BASE_DIR / 'static', '/static/app.css'))

    assert detail.status_code == 200
    assert detail.headers["content-encoding"] == "gzip"
    assert static.status_code == 200
    assert static.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_download_page_and_release_api_select_latest_builds(client, settings):
    (settings.releases_dir / "CodexBridge-v1.0.12.exe").write_bytes(b"old")
    windows = settings.releases_dir / "CodexBridge-v1.0.13.exe"
    android = settings.releases_dir / "CodexHub-v0.1.4-debug.apk"
    bybc_video = (
        settings.releases_dir
        / "BYBC_视频末帧延长工具_Setup_v1.0.0.exe"
    )
    windows.write_bytes(b"windows-current")
    android.write_bytes(b"android-current")
    bybc_video.write_bytes(b"bybc-video-current")

    manifest = client.get(
        "/api/releases", headers={"x-forwarded-prefix": "/hub"}
    )
    unauthenticated_page = client.get("/downloads", follow_redirects=False)
    login_owner(client)
    page = client.get("/downloads")
    download = client.get("/downloads/windows")
    bybc_video_download = client.get("/downloads/bybc-video")

    releases = {
        item["platform"]: item for item in manifest.json()["releases"]
    }
    assert manifest.status_code == 200
    assert releases["windows"]["version"] == "1.0.13"
    assert releases["windows"]["download_url"] == "/hub/downloads/windows"
    assert releases["android"]["version"] == "0.1.4"
    assert releases["bybc-video"]["version"] == "1.0.0"
    assert releases["bybc-video"]["platform_label"] == "BYBC 视频工具"
    assert releases["bybc-video"]["download_url"] == "/hub/downloads/bybc-video"
    assert manifest.json()["apple"]["status"] == "planned"
    assert unauthenticated_page.status_code == 303
    assert "下载 Windows 版" in page.text
    assert "下载 Android 版" in page.text
    assert "下载 BYBC 视频工具" in page.text
    assert "CodexBridge-v1.0.13.exe" in page.text
    assert "BYBC_视频末帧延长工具_Setup_v1.0.0.exe" in page.text
    assert download.content == b"windows-current"
    assert "CodexBridge-v1.0.13.exe" in download.headers["content-disposition"]
    assert bybc_video_download.content == b"bybc-video-current"
    assert quote("BYBC_视频末帧延长工具_Setup_v1.0.0.exe") in (
        bybc_video_download.headers["content-disposition"]
    )


def test_forwarded_prefix_is_used_for_static_links_and_redirects(client):
    response = client.get(
        "/login",
        headers={"x-forwarded-prefix": "/hub"},
    )
    login = client.post(
        "/login",
        data={"username": "owner", "password": "owner-password"},
        headers={"x-forwarded-prefix": "/hub"},
        follow_redirects=False,
    )

    assert 'href="/hub' + asset_url(BASE_DIR / 'static', '/static/app.css') + '"' in response.text
    assert 'src="/hub' + asset_url(BASE_DIR / 'static', '/static/pixel-auth.js') + '"' in response.text
    assert "hub-voice.js" not in response.text
    assert "image-viewer.js" not in response.text
    assert login.headers["location"] == "/hub/projects"


def test_untrusted_forwarded_prefix_cannot_create_external_redirect(client):
    response = client.get(
        "/",
        headers={"x-forwarded-prefix": "//evil.example"},
        follow_redirects=False,
    )
    traversal = client.get(
        "/login",
        headers={"x-forwarded-prefix": "/hub/../escape"},
    )

    assert response.headers["location"] == "/projects"
    assert 'href="' + asset_url(BASE_DIR / 'static', '/static/app.css') + '"' in traversal.text
    assert "evil.example" not in response.headers["location"]


def test_dynamic_authenticated_pages_disable_browser_caching(client):
    login_owner(client)

    response = client.get("/projects?list=1")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
