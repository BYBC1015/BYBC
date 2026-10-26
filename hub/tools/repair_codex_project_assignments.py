from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


def default_codex_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def normalize_windows_path(value: str) -> str:
    normalized = str(value or "").replace("/", "\\")
    if normalized.startswith("\\\\?\\"):
        normalized = normalized[4:]
    return normalized.rstrip("\\").casefold()


def project_for_cwd(
    cwd: str,
    project_roots: list[tuple[str, str, str]],
) -> tuple[str, str] | None:
    normalized_cwd = normalize_windows_path(cwd)
    matches = [
        (project_id, root)
        for normalized_root, project_id, root in project_roots
        if normalized_cwd == normalized_root
        or normalized_cwd.startswith(normalized_root + "\\")
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: len(normalize_windows_path(item[1])))


def load_threads(database_path: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(
        f"file:{database_path.as_posix()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, cwd, archived, updated_at_ms, updated_at
                FROM threads
                """
            )
        ]
    finally:
        connection.close()


def repair_state(
    state: dict[str, Any],
    threads: list[dict[str, Any]],
) -> dict[str, Any]:
    projects = state.get("local-projects")
    if not isinstance(projects, dict) or not projects:
        raise RuntimeError("Codex state has no local projects")

    assignments = state.setdefault("thread-project-assignments", {})
    hints = state.setdefault("thread-workspace-root-hints", {})
    orders = state.setdefault("sidebar-project-thread-orders", {})
    if not all(isinstance(item, dict) for item in (assignments, hints, orders)):
        raise RuntimeError("Codex project state has an unexpected shape")

    project_roots: list[tuple[str, str, str]] = []
    project_names: dict[str, str] = {}
    for project_id, project in projects.items():
        if not isinstance(project, dict):
            continue
        project_names[project_id] = str(project.get("name") or project_id)
        for root in project.get("rootPaths") or []:
            clean_root = str(root or "").rstrip("\\/")
            if clean_root:
                project_roots.append(
                    (normalize_windows_path(clean_root), project_id, clean_root)
                )
    project_roots.sort(key=lambda item: len(item[0]), reverse=True)

    added_by_project: dict[str, list[str]] = defaultdict(list)
    conflicts: list[dict[str, str]] = []
    thread_rows = {str(row["id"]): row for row in threads}

    for thread_id, row in thread_rows.items():
        match = project_for_cwd(str(row.get("cwd") or ""), project_roots)
        if match is None:
            continue
        project_id, root = match
        current = assignments.get(thread_id)
        if isinstance(current, dict):
            current_project_id = str(current.get("projectId") or "")
            if current_project_id and current_project_id != project_id:
                conflicts.append(
                    {
                        "threadId": thread_id,
                        "currentProjectId": current_project_id,
                        "matchedProjectId": project_id,
                    }
                )
            continue

        assignments[thread_id] = {
            "projectKind": "local",
            "projectId": project_id,
            "cwd": root,
            "pendingCoreUpdate": False,
        }
        hints[thread_id] = root
        added_by_project[project_id].append(thread_id)

    if conflicts:
        raise RuntimeError(
            "Refusing to overwrite conflicting assignments: "
            + json.dumps(conflicts[:10], ensure_ascii=False)
        )

    assigned_ids_by_project: dict[str, list[str]] = defaultdict(list)
    for thread_id, assignment in assignments.items():
        if not isinstance(assignment, dict):
            continue
        project_id = str(assignment.get("projectId") or "")
        if project_id in projects:
            assigned_ids_by_project[project_id].append(str(thread_id))

    def thread_sort_key(thread_id: str) -> tuple[int, str]:
        row = thread_rows.get(thread_id) or {}
        updated_at_ms = row.get("updated_at_ms")
        if isinstance(updated_at_ms, int):
            return updated_at_ms, thread_id
        return 0, str(row.get("updated_at") or thread_id)

    for project_id, thread_ids in assigned_ids_by_project.items():
        orders[project_id] = {
            "threadIds": sorted(
                set(thread_ids),
                key=thread_sort_key,
                reverse=True,
            )
        }

    summary_projects = []
    for project_id in projects:
        assigned_count = len(assigned_ids_by_project.get(project_id, []))
        active_count = sum(
            1
            for thread_id in assigned_ids_by_project.get(project_id, [])
            if not bool((thread_rows.get(thread_id) or {}).get("archived"))
        )
        summary_projects.append(
            {
                "projectId": project_id,
                "projectName": project_names.get(project_id, project_id),
                "added": len(added_by_project.get(project_id, [])),
                "assigned": assigned_count,
                "active": active_count,
            }
        )

    return {
        "added": sum(len(ids) for ids in added_by_project.values()),
        "assignmentCount": len(assignments),
        "projects": summary_projects,
    }


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    serialized = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=path.parent,
        prefix=f".{path.name}.repair-",
        suffix=".tmp",
        delete=False,
    ) as stream:
        temporary_path = Path(stream.name)
        stream.write(serialized)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary_path, path)


def main() -> int:
    codex_dir = default_codex_dir()
    parser = argparse.ArgumentParser(
        description=(
            "Restore unassigned Codex threads to local projects using exact "
            "state_5.sqlite cwd-to-project-root matches."
        )
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=codex_dir / ".codex-global-state.json",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=codex_dir / "state_5.sqlite",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the repaired state after creating a timestamped backup.",
    )
    arguments = parser.parse_args()

    state = json.loads(arguments.state.read_text(encoding="utf-8-sig"))
    threads = load_threads(arguments.database)
    summary = repair_state(state, threads)
    summary["statePath"] = str(arguments.state)
    summary["databasePath"] = str(arguments.database)
    summary["applied"] = bool(arguments.apply)

    if arguments.apply:
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        backup_path = arguments.state.with_name(
            f"{arguments.state.name}.project-assignment-repair-{timestamp}.bak"
        )
        shutil.copy2(arguments.state, backup_path)
        atomic_write_json(arguments.state, state)
        json.loads(arguments.state.read_text(encoding="utf-8"))
        summary["backupPath"] = str(backup_path)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
