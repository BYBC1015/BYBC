import json

import pytest

from codex_hub.repository import entry_routing
from codex_hub.server_ai import ServerAIRuntime


@pytest.fixture
def routing_project(repo):
    owner = repo.create_owner_if_missing("routing-owner", "password")
    primary = repo.create_device(label="codex-primary")
    helper = repo.create_device(label="hermes-helper")
    second = repo.create_device(label="codex-second")
    observer = repo.create_device(label="observer")
    project = repo.create_project(
        slug="routing", name="Routing", created_by_user_id=owner["id"]
    )
    for device in (primary, helper, second, observer):
        repo.add_project_member(
            project["id"], device["id"], "read" if device == observer else "write"
        )
    repo.set_project_primary_device(project["id"], primary["id"])
    repo.set_project_collaboration_mode(project["id"], True)
    return owner, project, primary, helper, second, observer


def user_entry(repo, setup, body="Answer this directly.", routing=None, **kwargs):
    owner, project, *_ = setup
    metadata = {} if routing is None else {"routing": routing}
    return repo.create_entry(
        project_id=project["id"], source_label="owner", kind="message",
        author_user_id=owner["id"], body_markdown=body,
        metadata_json=json.dumps(metadata), **kwargs,
    )


def delivered_ids(repo, setup, entry):
    return {
        device["id"] for device in setup[2:]
        if repo.get_mention_for_device(entry["id"], device["id"])
    }


@pytest.mark.parametrize("selected_indexes", [(3,), (3, 4), (2, 3, 4)])
def test_explicit_selection_excludes_every_other_device(repo, routing_project, selected_indexes):
    target_ids = [routing_project[index]["id"] for index in selected_indexes]
    entry = user_entry(
        repo, routing_project,
        body="@codex-primary @hermes-helper @codex-second are only text here.",
        routing={"mode": "selected", "device_ids": target_ids},
    )
    assert delivered_ids(repo, routing_project, entry) == set(target_ids)
    for device in routing_project[2:]:
        inbox_ids = {item["id"] for item in repo.list_mentions_for_device(device["id"])}
        assert (entry["id"] in inbox_ids) == (device["id"] in target_ids)
        if device["id"] not in target_ids:
            assert repo.claim_mention(entry["id"], device["id"]) is None
        else:
            delivery = repo.get_mention_for_device(entry["id"], device["id"])
            assert delivery["delivery_mode"] == "selected"
            assert delivery["target_device_ids"] == target_ids
            assert delivery["collaboration_enabled"] is False
            assert delivery["collaboration_role"] == ""


def test_project_group_preference_does_not_override_exact_mention_or_default(repo, routing_project):
    primary, helper = routing_project[2:4]
    direct = user_entry(repo, routing_project, "@hermes-helper please answer.")
    default = user_entry(repo, routing_project)
    assert delivered_ids(repo, routing_project, direct) == {helper["id"]}
    assert delivered_ids(repo, routing_project, default) == {primary["id"]}


def test_group_delivery_freezes_writable_members_at_send_time(repo, routing_project):
    _, project, primary, helper, second, observer = routing_project
    entry = user_entry(repo, routing_project, routing={"mode": "group", "device_ids": []})
    newcomer = repo.create_device(label="late-joiner")
    repo.add_project_member(project["id"], newcomer["id"], "write")
    repo.set_project_collaboration_mode(project["id"], False)
    assert delivered_ids(repo, routing_project, entry) == {primary["id"], helper["id"], second["id"]}
    assert repo.get_mention_for_device(entry["id"], newcomer["id"]) is None
    assert repo.get_mention_for_device(entry["id"], observer["id"]) is None
    assert repo.get_mention_for_device(entry["id"], helper["id"])["collaboration_role"] == "advisor"
    assert repo.get_mention_for_device(entry["id"], primary["id"])["collaboration_role"] == "primary"


def test_membership_or_primary_change_does_not_retarget_old_messages(repo, routing_project):
    _, project, primary, helper, *_ = routing_project
    entry = user_entry(repo, routing_project)
    repo.set_project_primary_device(project["id"], helper["id"])
    assert delivered_ids(repo, routing_project, entry) == {primary["id"]}
    repo.connection.execute(
        "DELETE FROM project_memberships WHERE project_id = ? AND device_id = ?",
        (project["id"], primary["id"]),
    )
    repo.connection.commit()
    assert repo.get_mention_for_device(entry["id"], primary["id"]) is None
    assert repo.claim_mention(entry["id"], primary["id"]) is None


def test_legacy_plain_history_is_not_replayed_by_group_toggle(repo, routing_project):
    _, project, *_ = routing_project
    entry = user_entry(repo, routing_project)
    repo.connection.execute("UPDATE entries SET metadata_json = '{}' WHERE id = ?", (entry["id"],))
    repo.connection.commit()
    repo.set_project_collaboration_mode(project["id"], False)
    repo.set_project_collaboration_mode(project["id"], True)
    assert delivered_ids(repo, routing_project, entry) == set()


def test_reply_to_selected_message_never_wakes_text_mentions(repo, routing_project):
    _, project, _, helper, *_ = routing_project
    entry = user_entry(repo, routing_project, routing={"mode": "selected", "device_ids": [helper["id"]]})
    reply = repo.create_entry(
        project["id"], helper["label"], "message",
        "@codex-primary @codex-second Here is the final answer.",
        author_device_id=helper["id"],
        metadata_json=json.dumps({"reply_to_entry_id": entry["id"]}),
    )
    assert entry_routing(reply) == {"mode": "none", "device_ids": []}
    assert delivered_ids(repo, routing_project, reply) == set()


def test_reply_link_cannot_cross_conversations_or_impersonate_a_selected_device(repo, routing_project):
    _, project, primary, helper, *_ = routing_project
    entry = user_entry(repo, routing_project, routing={"mode": "selected", "device_ids": [helper["id"]]})
    with pytest.raises(ValueError, match="not selected"):
        repo.create_entry(
            project["id"], primary["label"], "message", "Answer.",
            author_device_id=primary["id"], metadata_json=json.dumps({"reply_to_entry_id": entry["id"]}),
        )
    task = repo.create_task(project["id"], "Separate task", assigned_device_id=helper["id"])
    with pytest.raises(ValueError, match="this conversation"):
        repo.create_entry(
            project["id"], helper["label"], "message", "Answer.",
            author_device_id=helper["id"], task_id=task["id"],
            metadata_json=json.dumps({"reply_to_entry_id": entry["id"]}),
        )


def test_group_reply_can_notify_primary_once_without_recursive_group_discussion(repo, routing_project):
    _, project, primary, helper, *_ = routing_project
    entry = user_entry(repo, routing_project, routing={"mode": "group", "device_ids": []})
    reply = repo.create_entry(
        project["id"], helper["label"], "message", "@codex-second Advisor result.",
        author_device_id=helper["id"], metadata_json=json.dumps({"reply_to_entry_id": entry["id"]}),
    )
    assert delivered_ids(repo, routing_project, reply) == {primary["id"]}
    summary = repo.create_entry(
        project["id"], primary["label"], "message", "@hermes-helper Final summary.",
        author_device_id=primary["id"], metadata_json=json.dumps({"reply_to_entry_id": reply["id"]}),
    )
    assert delivered_ids(repo, routing_project, summary) == set()


@pytest.mark.parametrize("bad_ids", [[], [True], [0], ["2"], [999999]])
def test_explicit_invalid_targets_are_rejected_before_save(repo, routing_project, bad_ids):
    before = len(repo.list_all_entries(routing_project[1]["id"]))
    with pytest.raises(ValueError):
        user_entry(repo, routing_project, routing={"mode": "selected", "device_ids": bad_ids})
    assert len(repo.list_all_entries(routing_project[1]["id"])) == before


@pytest.mark.parametrize("body", ["Use the default device.", "@codex-primary @hermes-helper Do this."])
def test_explicit_empty_selection_never_falls_back_to_mentions_or_primary(repo, client, routing_project, body):
    _, project, primary, *_ = routing_project
    token = repo.create_api_token(primary["id"], "empty-routing")
    response = client.post(
        f"/api/projects/{project['slug']}/entries",
        headers={"Authorization": f"Bearer {token.raw}"},
        json={"body_markdown": body, "routing": {"mode": "selected", "device_ids": []}},
    )
    assert response.status_code == 422
    assert repo.list_all_entries(project["id"]) == []


def test_mobile_empty_selection_is_rejected_but_omitted_legacy_fields_still_route(repo, client, routing_project):
    owner, project, primary, helper, *_ = routing_project
    login = client.post(
        "/login", data={"username": owner["username"], "password": "password"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    payload = {
        "_csrf": client.cookies.get("codex_hub_csrf"), "kind": "message",
        "body_markdown": "@hermes-helper Handle this.",
    }
    empty = client.post(
        f"/projects/{project['slug']}/entries", data={**payload, "delivery_mode": "selected"},
        headers={"x-requested-with": "codex-hub-composer"},
    )
    assert empty.status_code == 422
    assert repo.list_all_entries(project["id"]) == []
    legacy = client.post(
        f"/projects/{project['slug']}/entries", data=payload,
        headers={"x-requested-with": "codex-hub-composer"},
    )
    assert legacy.status_code == 201
    entry = legacy.json()["entry"]
    assert repo.get_mention_for_device(entry["id"], helper["id"])
    assert repo.get_mention_for_device(entry["id"], primary["id"]) is None


def test_read_only_member_can_receive_legacy_mentions_but_cannot_be_explicit_executor(repo, routing_project):
    observer = routing_project[5]
    with pytest.raises(ValueError, match="writable"):
        user_entry(repo, routing_project, routing={"mode": "selected", "device_ids": [observer["id"]]})
    mention = user_entry(repo, routing_project, "@observer please read this.")
    assert delivered_ids(repo, routing_project, mention) == {observer["id"]}


def test_task_default_uses_assignee_and_does_not_notify_project_primary(repo, routing_project):
    _, project, _, helper, *_ = routing_project
    task = repo.create_task(project["id"], "Direct task", assigned_device_id=helper["id"])
    entry = user_entry(repo, routing_project, task_id=task["id"])
    assert delivered_ids(repo, routing_project, entry) == {helper["id"]}
    prompt = repo.build_task_prompt_markdown(project["slug"], task["id"])
    assert "Handle this task directly" in prompt
    assert "include @codex-primary in your Hub reply" not in prompt


def test_direct_device_selection_does_not_add_unrequested_project_roles(repo, routing_project):
    _, project, _, helper, *_ = routing_project
    repo.add_project_chat_role(project["id"], "advisor", helper["id"], "Always recommend alternatives.")
    entry = user_entry(repo, routing_project, routing={"mode": "selected", "device_ids": [helper["id"]]})
    assert repo.get_mention_for_device(entry["id"], helper["id"])["group_chat_roles"] == []


def test_direct_delivery_hides_old_bridge_primary_and_group_activation_hints(repo, routing_project):
    _, project, primary, helper, *_ = routing_project
    repo.add_project_chat_role(project["id"], "advisor", helper["id"], "Review as an advisor.")
    entry = user_entry(
        repo, routing_project, "@advisor Provide the answer yourself.",
        routing={"mode": "selected", "device_ids": [helper["id"]]},
    )
    inbox_delivery = repo.list_mentions_for_device(helper["id"])[0]
    claimed_delivery = repo.get_mention_for_device(entry["id"], helper["id"])
    for delivery in (inbox_delivery, claimed_delivery):
        assert delivery["primary_device_label"] == ""
        assert delivery["project_primary_device_id"] == primary["id"]
        assert delivery["group_chat_roles"] == []
        assert delivery["collaboration_role"] == ""
        assert delivery["collaboration_enabled"] is False
    group_entry = user_entry(repo, routing_project, routing={"mode": "group", "device_ids": []})
    group_delivery = repo.get_mention_for_device(group_entry["id"], helper["id"])
    assert group_delivery["primary_device_label"] == primary["label"]
    assert group_delivery["group_chat_roles"][0]["name"] == "advisor"
    assert group_delivery["collaboration_role"] == "advisor"


def test_server_model_selection_excludes_primary_and_other_models_and_reply_mentions(repo, routing_project, settings):
    _, project, primary, *_ = routing_project
    selected = repo.ensure_server_ai_device("deepseek", "DeepSeek")
    other = repo.ensure_server_ai_device("kimi", "Kimi")
    for device in (selected, other):
        repo.add_project_member(project["id"], device["id"], "write")
    entry = user_entry(
        repo, routing_project, "@codex-primary @Kimi These mentions are context.",
        routing={"mode": "selected", "device_ids": [selected["id"]]},
    )
    jobs = repo.enqueue_server_ai_jobs_for_entry(entry["id"], {"deepseek", "kimi"})
    assert [job["target_device_id"] for job in jobs] == [selected["id"]]
    assert repo.get_mention_for_device(entry["id"], primary["id"]) is None
    runtime = ServerAIRuntime(settings)
    messages = runtime._build_messages(repo, jobs[0], runtime.providers["deepseek"])
    assert "selected you to answer directly" in messages[0]["content"]
    response = repo.finish_server_ai_job(jobs[0]["id"], "@codex-primary @Kimi The result.", "{}")
    assert entry_routing(response)["mode"] == "none"
    assert repo.get_mention_for_device(response["id"], primary["id"]) is None
    assert repo.get_mention_for_device(response["id"], other["id"]) is None
    assert repo.enqueue_server_ai_jobs_for_entry(response["id"], {"deepseek", "kimi"}) == []


def test_attachment_message_obeys_same_explicit_scope(repo, routing_project):
    owner, project, primary, helper, *_ = routing_project
    entry, _ = repo.create_entry_with_attachments(
        project_id=project["id"], source_label="owner", kind="message",
        author_user_id=owner["id"], body_markdown="@codex-primary", attachments=[],
        metadata_json=json.dumps({"routing": {"mode": "selected", "device_ids": [helper["id"]]}}),
    )
    assert repo.get_mention_for_device(entry["id"], primary["id"]) is None
    assert repo.get_mention_for_device(entry["id"], helper["id"])


def test_project_api_preserves_reply_scope(repo, client, routing_project):
    _, project, primary, helper, *_ = routing_project
    token = repo.create_api_token(helper["id"], "routing-api")
    entry = user_entry(repo, routing_project, routing={"mode": "selected", "device_ids": [helper["id"]]})
    response = client.post(
        f"/api/projects/{project['slug']}/entries",
        headers={"Authorization": f"Bearer {token.raw}"},
        json={"body_markdown": "@codex-primary Finished.", "reply_to_entry_id": entry["id"]},
    )
    assert response.status_code == 201
    reply = response.json()["entry"]
    assert entry_routing(reply)["mode"] == "none"
    assert repo.get_mention_for_device(reply["id"], primary["id"]) is None


def test_task_api_preserves_reply_scope(repo, client, routing_project):
    _, project, primary, helper, *_ = routing_project
    token = repo.create_api_token(helper["id"], "task-routing-api")
    task = repo.create_task(project["id"], "Scoped task", assigned_device_id=helper["id"])
    entry = user_entry(
        repo, routing_project, routing={"mode": "selected", "device_ids": [helper["id"]]},
        task_id=task["id"],
    )
    response = client.post(
        f"/api/projects/{project['slug']}/tasks/{task['id']}/entries",
        headers={"Authorization": f"Bearer {token.raw}"},
        json={"body_markdown": "@codex-primary Finished.", "reply_to_entry_id": entry["id"]},
    )
    assert response.status_code == 201
    reply = response.json()["entry"]
    assert entry_routing(reply)["mode"] == "none"
    assert repo.get_mention_for_device(reply["id"], primary["id"]) is None
