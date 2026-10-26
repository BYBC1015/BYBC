import pytest

from codex_hub.project_memory import (
    compare_project_manifests,
    normalize_project_manifest,
    render_project_memory_markdown,
    render_runtime_project_memory_markdown,
)


def file_entry(path: str, marker: str, size: int = 10) -> dict:
    return {"path": path, "sha256": marker * 64, "size": size}


def test_project_manifest_is_portable_and_rejects_traversal():
    manifest = normalize_project_manifest(
        {
            "git_commit": "abc123",
            "files": [file_entry("docs\\PROJECT_MEMORY.md", "a")],
        }
    )

    assert manifest["schema_version"] == 1
    assert manifest["files"][0]["path"] == "docs/PROJECT_MEMORY.md"
    with pytest.raises(ValueError):
        normalize_project_manifest({"files": [file_entry("../secret.txt", "b")]})


def test_project_manifest_diff_only_requests_missing_and_changed_files():
    source = {
        "files": [
            file_entry("PROJECT_MEMORY.md", "a", 12),
            file_entry("data/large.bin", "b", 120 * 1024 * 1024),
        ]
    }
    target = {
        "files": [
            file_entry("PROJECT_MEMORY.md", "c", 11),
            file_entry("notes/local.md", "d", 3),
        ]
    }

    diff = compare_project_manifests(source, target)

    assert [item["path"] for item in diff["missing_on_target"]] == [
        "data/large.bin"
    ]
    assert [item["path"] for item in diff["missing_on_source"]] == [
        "notes/local.md"
    ]
    assert [item["path"] for item in diff["changed"]] == ["PROJECT_MEMORY.md"]
    assert diff["transfer_candidate_count"] == 2
    assert diff["requires_large_transfer_confirmation"] is True


def test_project_memory_versions_handoffs_and_device_diff(repo):
    user = repo.create_owner_if_missing("owner", "password")
    primary = repo.create_device(label="codex-device-a")
    secondary = repo.create_device(label="codex-device-b")
    project = repo.create_project(
        slug="memory-sync",
        name="Memory Sync",
        created_by_user_id=user["id"],
    )
    repo.add_project_member(project["id"], primary["id"], "admin")
    repo.add_project_member(project["id"], secondary["id"], "write")
    repo.set_project_primary_device(project["id"], primary["id"])
    memory = repo.save_project_control(
        project_id=project["id"],
        goal="Continue cleanly on every device",
        architecture="Hub memory plus local mirror",
        known_issues="Secondary device is missing one data record",
        user_preferences="Create a fresh Codex conversation per item",
        next_steps="Compare manifests before transfer",
        created_by_user_id=user["id"],
    )
    handoff = repo.create_project_handoff(
        project["id"],
        title="Switch primary device",
        summary_markdown="Primary implementation is complete.",
        source_device_id=primary["id"],
        target_device_id=secondary["id"],
        manifest={"files": [file_entry("PROJECT_MEMORY.md", "a", 12)]},
        created_by_user_id=user["id"],
    )
    primary_snapshot = repo.save_project_sync_snapshot(
        project["id"],
        primary["id"],
        memory_version=memory["version"],
        git_commit="primary-head",
        manifest={
            "files": [
                file_entry("PROJECT_MEMORY.md", "a", 12),
                file_entry("records/new.md", "b", 20),
            ]
        },
    )
    secondary_snapshot = repo.save_project_sync_snapshot(
        project["id"],
        secondary["id"],
        memory_version=0,
        git_commit="secondary-head",
        manifest={"files": [file_entry("PROJECT_MEMORY.md", "c", 10)]},
    )

    diff = repo.compare_project_devices(
        project["id"], primary["id"], secondary["id"]
    )
    accepted = repo.accept_project_handoff(
        project["id"], handoff["id"], accepted_by_user_id=user["id"]
    )
    markdown = render_project_memory_markdown(
        project, memory, repo.list_project_handoffs(project["id"])
    )
    runtime_markdown = render_runtime_project_memory_markdown(project, memory)

    assert handoff["version"] == 1
    assert accepted["status"] == "accepted"
    assert primary_snapshot["revision"] == 1
    assert secondary_snapshot["revision"] == 1
    assert diff["source_memory_version"] == 1
    assert [item["path"] for item in diff["missing_on_target"]] == [
        "records/new.md"
    ]
    assert [item["path"] for item in diff["changed"]] == ["PROJECT_MEMORY.md"]
    assert "Continue cleanly on every device" in markdown
    assert "Switch primary device" not in markdown
    assert "Primary implementation is complete." in markdown
    assert "Continue cleanly on every device" in runtime_markdown
    assert "Recent Handoffs" not in runtime_markdown
    assert "Memory Use" not in runtime_markdown
    assert "Architecture:" not in runtime_markdown
    assert ".codex-hub/PROJECT_MEMORY.md" in runtime_markdown
    assert len(runtime_markdown) < len(markdown)
