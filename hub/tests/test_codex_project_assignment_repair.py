import pytest

from tools.repair_codex_project_assignments import (
    normalize_windows_path,
    repair_state,
)


def sample_state():
    return {
        "local-projects": {
            "project-one": {
                "id": "project-one",
                "name": "One",
                "rootPaths": [r"C:\Work\One"],
            },
            "project-two": {
                "id": "project-two",
                "name": "Two",
                "rootPaths": [r"D:\Work\Two"],
            },
        },
        "thread-project-assignments": {
            "existing": {
                "projectKind": "local",
                "projectId": "project-one",
                "cwd": r"C:\Work\One",
                "pendingCoreUpdate": False,
            }
        },
        "thread-workspace-root-hints": {},
        "sidebar-project-thread-orders": {},
    }


def test_normalize_windows_path_handles_extended_prefix_and_case():
    assert normalize_windows_path(r"\\?\C:\Work\One\\") == r"c:\work\one"


def test_repair_assigns_only_exact_project_root_descendants_and_rebuilds_order():
    state = sample_state()
    threads = [
        {
            "id": "existing",
            "cwd": r"C:\Work\One",
            "archived": 0,
            "updated_at_ms": 10,
            "updated_at": "2026-01-01",
        },
        {
            "id": "new-active",
            "cwd": r"\\?\C:\Work\One\subdir",
            "archived": 0,
            "updated_at_ms": 30,
            "updated_at": "2026-01-03",
        },
        {
            "id": "new-archived",
            "cwd": r"D:\Work\Two",
            "archived": 1,
            "updated_at_ms": 20,
            "updated_at": "2026-01-02",
        },
        {
            "id": "unmatched",
            "cwd": r"E:\Elsewhere",
            "archived": 0,
            "updated_at_ms": 40,
            "updated_at": "2026-01-04",
        },
    ]

    summary = repair_state(state, threads)

    assert summary["added"] == 2
    assert (
        state["thread-project-assignments"]["new-active"]["projectId"]
        == "project-one"
    )
    assert (
        state["thread-project-assignments"]["new-archived"]["projectId"]
        == "project-two"
    )
    assert "unmatched" not in state["thread-project-assignments"]
    assert state["thread-workspace-root-hints"]["new-active"] == r"C:\Work\One"
    assert state["sidebar-project-thread-orders"]["project-one"]["threadIds"] == [
        "new-active",
        "existing",
    ]
    projects = {item["projectId"]: item for item in summary["projects"]}
    assert projects["project-one"]["assigned"] == 2
    assert projects["project-one"]["active"] == 2
    assert projects["project-two"]["assigned"] == 1
    assert projects["project-two"]["active"] == 0


def test_repair_refuses_to_overwrite_a_different_existing_project():
    state = sample_state()
    state["thread-project-assignments"]["conflict"] = {
        "projectKind": "local",
        "projectId": "project-two",
        "cwd": r"D:\Work\Two",
        "pendingCoreUpdate": False,
    }
    threads = [
        {
            "id": "conflict",
            "cwd": r"C:\Work\One",
            "archived": 0,
            "updated_at_ms": 10,
            "updated_at": "2026-01-01",
        }
    ]

    with pytest.raises(RuntimeError, match="conflicting assignments"):
        repair_state(state, threads)
