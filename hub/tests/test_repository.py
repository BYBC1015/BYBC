def test_schema_creates_core_tables(db):
    names = {
        row["name"]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }

    assert {
        "users",
        "sessions",
        "device_groups",
        "devices",
        "api_tokens",
        "projects",
        "project_memberships",
        "project_chat_roles",
        "entries",
        "attachments",
        "audit_events",
    }.issubset(names)


def test_schema_creates_coordination_tables(db):
    names = {
        row["name"]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    entry_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(entries)").fetchall()
    }
    device_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(devices)").fetchall()
    }
    project_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(projects)").fetchall()
    }

    assert {
        "project_control_documents",
        "project_handoffs",
        "project_sync_snapshots",
        "tasks",
        "mention_receipts",
    }.issubset(names)
    assert "task_id" in entry_columns
    assert {"last_web_seen_at", "last_agent_seen_at"}.issubset(device_columns)
    assert "primary_device_id" in project_columns


def test_project_creator_device_gets_write_membership(repo):
    device = repo.create_device(label="laptop-a")
    project = repo.create_project(
        slug="proposal",
        name="Proposal",
        created_by_device_id=device["id"],
    )

    membership = repo.get_project_membership(project["id"], device["id"])

    assert membership["permission"] == "write"
    assert project["primary_device_id"] == device["id"]


def test_project_primary_device_must_be_project_member(repo):
    primary = repo.create_device(label="codex-device-a")
    helper = repo.create_device(label="hermes-device-a")
    project = repo.create_project(
        slug="primary-device",
        name="Primary Device",
        created_by_device_id=primary["id"],
    )

    updated = repo.set_project_primary_device(project["id"], primary["id"])

    assert updated["primary_device_id"] == primary["id"]
    assert repo.get_project_primary_device(project["id"])["label"] == "codex-device-a"
    try:
        repo.set_project_primary_device(project["id"], helper["id"])
    except ValueError as exc:
        assert "primary device" in str(exc)
    else:
        raise AssertionError("non-member helper should not become primary")


def test_project_chat_role_targets_its_writable_device(repo):
    owner = repo.create_owner_if_missing("owner", "password")
    primary = repo.create_device(label="codex-device-a")
    project = repo.create_project(
        slug="role-chat",
        name="Role Chat",
        created_by_user_id=owner["id"],
    )
    repo.add_project_member(
        project_id=project["id"],
        device_id=primary["id"],
        permission="write",
        added_by_user_id=owner["id"],
    )
    role = repo.add_project_chat_role(
        project_id=project["id"],
        name="安全审查",
        device_id=primary["id"],
        instructions="检查权限、输入和数据边界。",
        added_by_user_id=owner["id"],
    )
    entry = repo.create_entry(
        project_id=project["id"],
        author_user_id=owner["id"],
        source_label="owner",
        kind="message",
        body_markdown="@安全审查 请检查方案。",
    )

    mentions = repo.list_mentions_for_device(primary["id"])

    assert role["device_label"] == "codex-device-a"
    assert [item["id"] for item in mentions] == [entry["id"]]
    assert mentions[0]["matched_targets"] == ["安全审查"]
    assert mentions[0]["delivery_mode"] == "selected"
    assert mentions[0]["group_chat_roles"] == []
    assert "数据边界" in role["instructions"]


def test_project_chat_role_requires_writable_member_and_is_removed_with_device(repo):
    owner = repo.create_owner_if_missing("owner", "password")
    observer = repo.create_device(label="observer")
    project = repo.create_project(slug="role-guard", name="Role Guard")
    repo.add_project_member(project["id"], observer["id"], "read")

    try:
        repo.add_project_chat_role(project["id"], "reviewer", observer["id"])
    except ValueError as exc:
        assert "writable" in str(exc)
    else:
        raise AssertionError("read-only member should not execute a chat role")

    repo.add_project_member(project["id"], observer["id"], "write")
    repo.add_project_chat_role(
        project["id"], "reviewer", observer["id"], added_by_user_id=owner["id"]
    )
    repo.remove_project_member(project["id"], observer["id"])

    assert repo.list_project_chat_roles(project["id"]) == []


def test_api_token_auth_updates_device_presence_for_project_memberships(repo):
    device = repo.create_device(label="laptop-b")
    token = repo.create_api_token(device_id=device["id"], name="main")
    project = repo.create_project(
        slug="presence",
        name="Presence",
        created_by_device_id=device["id"],
    )

    before = repo.list_project_memberships(project["id"])[0]
    repo.authenticate_api_token(token.raw)
    after = repo.list_project_memberships(project["id"])[0]

    assert before["last_seen_at"] is None
    assert before["last_agent_seen_at"] is None
    assert after["last_seen_at"] is not None
    assert after["last_agent_seen_at"] is not None


def test_unlisted_device_cannot_read_project_entries(repo):
    allowed = repo.create_device(label="allowed")
    blocked = repo.create_device(label="blocked")
    project = repo.create_project(
        slug="private",
        name="Private",
        created_by_device_id=allowed["id"],
    )
    repo.create_entry(
        project_id=project["id"],
        author_device_id=allowed["id"],
        source_label="allowed",
        kind="markdown",
        body_markdown="secret context",
    )

    assert repo.can_device_read(project["id"], allowed["id"])
    assert not repo.can_device_read(project["id"], blocked["id"])


def test_project_control_document_versions(repo):
    user = repo.create_owner_if_missing("owner", "password")
    project = repo.create_project(
        slug="control",
        name="Control",
        created_by_user_id=user["id"],
    )

    first = repo.save_project_control(
        project_id=project["id"],
        goal="Ship coordination hub",
        non_goals="No local watcher yet",
        constraints_text="Do not touch SW upload",
        current_phase="MVP",
        acceptance="Tasks can be claimed",
        roles="Device A implements, Device B reviews",
        decisions="Use server-rendered UI",
        risks="Token waste",
        handoff_summary="Read PCD first",
        created_by_user_id=user["id"],
    )
    second = repo.save_project_control(
        project_id=project["id"],
        goal="Updated",
        created_by_user_id=user["id"],
    )

    assert first["version"] == 1
    assert second["version"] == 2
    assert repo.get_latest_project_control(project["id"])["goal"] == "Updated"


def test_task_claim_requires_write_membership_and_is_exclusive(repo):
    allowed = repo.create_device(label="allowed")
    blocked = repo.create_device(label="blocked")
    project = repo.create_project(
        slug="tasks",
        name="Tasks",
        created_by_device_id=allowed["id"],
    )
    task = repo.create_task(project_id=project["id"], title="Implement UI")

    claimed = repo.claim_task(task["id"], allowed["id"])

    assert claimed["claimed_by_device_id"] == allowed["id"]
    assert claimed["status"] == "claimed"
    assert repo.claim_task(task["id"], blocked["id"]) is None


def test_task_assignment_requires_project_allowlist(repo):
    allowed = repo.create_device(label="allowed")
    blocked = repo.create_device(label="blocked")
    project = repo.create_project(
        slug="assignment",
        name="Assignment",
        created_by_device_id=allowed["id"],
    )

    task = repo.create_task(
        project_id=project["id"],
        title="Assigned to allowed",
        assigned_device_id=allowed["id"],
    )

    assert task["assigned_device_id"] == allowed["id"]
    try:
        repo.create_task(
            project_id=project["id"],
            title="Assigned to blocked",
            assigned_device_id=blocked["id"],
        )
    except ValueError as exc:
        assert "allowlist" in str(exc)
    else:
        raise AssertionError("assignment outside project allowlist should fail")


def test_task_prompt_uses_bounded_context_without_secret_material(repo):
    device = repo.create_device(label="device-a")
    token = repo.create_api_token(device_id=device["id"], name="main")
    project = repo.create_project(
        slug="prompt",
        name="Prompt",
        created_by_device_id=device["id"],
    )
    repo.save_project_control(
        project_id=project["id"],
        goal="Build safely",
        constraints_text="Do not touch /upload",
    )
    task = repo.create_task(
        project_id=project["id"],
        title="Review deployment",
        description="Check service status",
        context_pack_markdown="Only inspect Codex Hub.",
    )
    other_task = repo.create_task(
        project_id=project["id"],
        title="Unrelated",
        description="Should not be exported",
    )
    task_entry = repo.create_entry(
        project_id=project["id"],
        task_id=task["id"],
        author_device_id=device["id"],
        source_label="device-a",
        kind="message",
        body_markdown="I found the likely path.",
    )
    other_task_entry = repo.create_entry(
        project_id=project["id"],
        task_id=other_task["id"],
        author_device_id=device["id"],
        source_label="device-a",
        kind="message",
        body_markdown="Unrelated long history.",
    )
    project_entry = repo.create_entry(
        project_id=project["id"],
        author_device_id=device["id"],
        source_label="device-a",
        kind="message",
        body_markdown="Project chat must stay outside the task prompt.",
    )
    for entry, filename in (
        (task_entry, "task-only.txt"),
        (other_task_entry, "other-task.txt"),
        (project_entry, "project-chat.txt"),
    ):
        repo.create_attachment(
            project_id=project["id"],
            entry_id=entry["id"],
            original_filename=filename,
            stored_relpath=f"test/{filename}",
            content_type="text/plain",
            byte_size=10,
            sha256="a" * 64,
            created_by_device_id=device["id"],
        )

    prompt = repo.build_task_prompt_markdown(project["slug"], task["id"])

    assert "Build safely" in prompt
    assert "Do not touch /upload" in prompt
    assert "Review deployment" in prompt
    assert "Only inspect Codex Hub." in prompt
    assert "I found the likely path." in prompt
    assert "Unrelated long history." not in prompt
    assert "Project chat must stay outside the task prompt." not in prompt
    assert "task-only.txt" in prompt
    assert "other-task.txt" not in prompt
    assert "project-chat.txt" not in prompt
    assert token.raw not in prompt


def test_mentions_match_exact_device_or_group_tokens(repo):
    owner = repo.create_owner_if_missing("owner", "password")
    group = repo.create_group("codex-default")
    device_b = repo.create_device(label="codex-device-b", group_id=group["id"])
    device_b2 = repo.create_device(label="codex-device-b2")
    project = repo.create_project(
        slug="mention-boundary",
        name="Mention Boundary",
        created_by_user_id=owner["id"],
    )
    repo.add_project_member(
        project_id=project["id"],
        device_id=device_b["id"],
        permission="write",
        added_by_user_id=owner["id"],
    )
    repo.add_project_member(
        project_id=project["id"],
        device_id=device_b2["id"],
        permission="write",
        added_by_user_id=owner["id"],
    )
    repo.create_entry(
        project_id=project["id"],
        author_user_id=owner["id"],
        source_label="owner",
        kind="message",
        body_markdown="@codex-device-b2 should not wake device b",
    )
    repo.create_entry(
        project_id=project["id"],
        author_user_id=owner["id"],
        source_label="owner",
        kind="message",
        body_markdown="@codex-device-b, exact device mention",
    )
    repo.create_entry(
        project_id=project["id"],
        author_user_id=owner["id"],
        source_label="owner",
        kind="message",
        body_markdown="@codex-default group mention",
    )

    device_b_mentions = [
        item["body_markdown"] for item in repo.list_mentions_for_device(device_b["id"])
    ]
    device_b2_mentions = [
        item["body_markdown"] for item in repo.list_mentions_for_device(device_b2["id"])
    ]

    assert "@codex-device-b, exact device mention" in device_b_mentions
    assert "@codex-default group mention" in device_b_mentions
    assert "@codex-device-b2 should not wake device b" not in device_b_mentions
    assert device_b2_mentions == ["@codex-device-b2 should not wake device b"]


def test_mentions_include_targets_and_server_handled_state(repo):
    owner = repo.create_owner_if_missing("owner", "password")
    group = repo.create_group("codex-default")
    device = repo.create_device(label="codex-device-b", group_id=group["id"])
    project = repo.create_project(
        slug="mention-state",
        name="Mention State",
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
        body_markdown="@codex-device-b and @codex-default please check this",
    )

    mention = repo.list_mentions_for_device(device["id"])[0]

    assert mention["mention_targets"] == ["codex-default", "codex-device-b"]
    assert mention["matched_targets"] == ["codex-default", "codex-device-b"]
    assert mention["claim_status"] == "unclaimed"
    assert mention["claimed_at"] is None
    assert mention["handled_at"] is None

    claimed = repo.claim_mention(entry["id"], device["id"])
    assert claimed["status"] == "claimed"
    assert claimed["delivery_id"]

    dispatching = repo.mark_mention_dispatching(entry["id"], device["id"])
    assert dispatching["status"] == "dispatching"
    assert dispatching["dispatching_at"] is not None
    assert dispatching["delivery_id"] == claimed["delivery_id"]

    submitted = repo.mark_mention_submitted(entry["id"], device["id"])
    assert submitted["status"] == "submitted"
    assert submitted["submitted_at"] is not None
    assert repo.release_mention_dispatch(entry["id"], device["id"])["status"] == "submitted"

    claimed_mention = repo.list_mentions_for_device(device["id"])[0]
    assert claimed_mention["claim_status"] == "submitted"
    assert claimed_mention["claimed_at"] is not None
    assert claimed_mention["delivery_id"] == claimed["delivery_id"]

    handled = repo.mark_mention_handled(entry["id"], device["id"])
    assert handled["status"] == "handled"
    assert handled["handled_at"] is not None
    assert repo.list_mentions_for_device(device["id"]) == []

    handled_mention = repo.list_mentions_for_device(
        device["id"], include_handled=True
    )[0]
    assert handled_mention["claim_status"] == "handled"
    assert handled_mention["handled_at"] == handled["handled_at"]


def test_mention_claim_requires_targeted_project_member(repo):
    owner = repo.create_owner_if_missing("owner", "password")
    targeted = repo.create_device(label="codex-device-b")
    untargeted = repo.create_device(label="codex-device-c")
    outsider = repo.create_device(label="codex-device-d")
    project = repo.create_project(
        slug="mention-claim-boundary",
        name="Mention Claim Boundary",
        created_by_user_id=owner["id"],
    )
    repo.add_project_member(
        project_id=project["id"],
        device_id=targeted["id"],
        permission="write",
        added_by_user_id=owner["id"],
    )
    repo.add_project_member(
        project_id=project["id"],
        device_id=untargeted["id"],
        permission="write",
        added_by_user_id=owner["id"],
    )
    entry = repo.create_entry(
        project_id=project["id"],
        author_user_id=owner["id"],
        source_label="owner",
        kind="message",
        body_markdown="@codex-device-b please check this",
    )

    assert repo.claim_mention(entry["id"], targeted["id"]) is not None
    assert repo.claim_mention(entry["id"], untargeted["id"]) is None
    assert repo.claim_mention(entry["id"], outsider["id"]) is None


def test_mobile_notifications_return_only_device_entries_after_cursor(repo):
    owner = repo.create_owner_if_missing("owner", "password")
    device = repo.create_device(label="mobile-device")
    project = repo.create_project(
        slug="mobile-feed",
        name="Mobile Feed",
        created_by_user_id=owner["id"],
    )
    user_entry = repo.create_entry(
        project_id=project["id"],
        author_user_id=owner["id"],
        source_label="owner",
        kind="message",
        body_markdown="user message",
    )
    first_reply = repo.create_entry(
        project_id=project["id"],
        author_device_id=device["id"],
        source_label="mobile-device",
        kind="message",
        body_markdown="first reply",
    )
    second_reply = repo.create_entry(
        project_id=project["id"],
        author_device_id=device["id"],
        source_label="mobile-device",
        kind="message",
        body_markdown="second reply",
    )

    latest, notifications = repo.list_mobile_notifications(
        after_entry_id=first_reply["id"]
    )

    assert user_entry["id"] < first_reply["id"]
    assert latest == second_reply["id"]
    assert [item["id"] for item in notifications] == [second_reply["id"]]
    assert notifications[0]["project_slug"] == "mobile-feed"


def test_mobile_notifications_page_without_skipping_a_large_backlog(repo):
    owner = repo.create_owner_if_missing("owner", "password")
    device = repo.create_device(label="mobile-backlog-device")
    project = repo.create_project(
        slug="mobile-backlog",
        name="Mobile Backlog",
        created_by_user_id=owner["id"],
    )
    starting_entry = repo.create_entry(
        project_id=project["id"],
        author_user_id=owner["id"],
        source_label="owner",
        kind="message",
        body_markdown="start",
    )
    replies = [
        repo.create_entry(
            project_id=project["id"],
            author_device_id=device["id"],
            source_label="mobile-backlog-device",
            kind="message",
            body_markdown=f"reply {index}",
        )
        for index in range(3)
    ]
    trailing_user_entry = repo.create_entry(
        project_id=project["id"],
        author_user_id=owner["id"],
        source_label="owner",
        kind="message",
        body_markdown="after replies",
    )

    first_cursor, first_page = repo.list_mobile_notifications(
        after_entry_id=starting_entry["id"], limit=2
    )
    second_cursor, second_page = repo.list_mobile_notifications(
        after_entry_id=first_cursor, limit=2
    )

    assert [item["id"] for item in first_page] == [
        replies[0]["id"],
        replies[1]["id"],
    ]
    assert first_cursor == replies[1]["id"]
    assert [item["id"] for item in second_page] == [replies[2]["id"]]
    assert second_cursor == trailing_user_entry["id"]


def test_live_chat_entries_page_without_skipping_a_large_backlog(repo):
    owner = repo.create_owner_if_missing("owner", "password")
    project = repo.create_project(
        slug="live-chat-feed",
        name="Live Chat Feed",
        created_by_user_id=owner["id"],
    )
    starting_entry = repo.create_entry(
        project_id=project["id"],
        author_user_id=owner["id"],
        source_label="owner",
        kind="message",
        body_markdown="start",
    )
    updates = [
        repo.create_entry(
            project_id=project["id"],
            author_user_id=owner["id"],
            source_label="owner",
            kind="message",
            body_markdown=f"update {index}",
        )
        for index in range(3)
    ]

    latest, first_cursor, first_has_more, first_page = repo.list_entries_after(
        project["id"], after_entry_id=starting_entry["id"], limit=2
    )
    _, second_cursor, second_has_more, second_page = repo.list_entries_after(
        project["id"], after_entry_id=first_cursor, limit=2
    )

    assert latest == updates[-1]["id"]
    assert first_has_more is True
    assert [item["id"] for item in first_page] == [
        updates[0]["id"],
        updates[1]["id"],
    ]
    assert first_cursor == updates[1]["id"]
    assert second_has_more is False
    assert [item["id"] for item in second_page] == [updates[2]["id"]]
    assert second_cursor == updates[-1]["id"]


def test_project_chat_queries_exclude_task_thread_entries(repo):
    owner = repo.create_owner_if_missing("owner", "password")
    project = repo.create_project(
        slug="entry-scope",
        name="Entry Scope",
        created_by_user_id=owner["id"],
    )
    task = repo.create_task(
        project_id=project["id"],
        title="Private task thread",
    )
    project_entry = repo.create_entry(
        project_id=project["id"],
        author_user_id=owner["id"],
        source_label="owner",
        kind="message",
        body_markdown="project-only",
    )
    task_entry = repo.create_entry(
        project_id=project["id"],
        task_id=task["id"],
        author_user_id=owner["id"],
        source_label="owner",
        kind="message",
        body_markdown="task-only",
    )

    project_entries = repo.list_project_entries(project["id"])
    latest, cursor, has_more, updates = repo.list_entries_after(project["id"])
    task_entries = repo.list_entries(project["id"], task_id=task["id"])
    all_entries = repo.list_all_entries(project["id"])

    assert [entry["id"] for entry in project_entries] == [project_entry["id"]]
    assert [entry["id"] for entry in updates] == [project_entry["id"]]
    assert latest == project_entry["id"]
    assert cursor == project_entry["id"]
    assert has_more is False
    assert [entry["id"] for entry in task_entries] == [task_entry["id"]]
    assert {entry["id"] for entry in all_entries} == {
        project_entry["id"],
        task_entry["id"],
    }
