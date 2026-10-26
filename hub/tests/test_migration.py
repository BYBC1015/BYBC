from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from codex_hub.config import Settings
from codex_hub.db import connect_database, initialize_schema
from codex_hub.migration import (
    BACKUP_FORMAT,
    BACKUP_FORMAT_VERSION,
    MANIFEST_NAME,
    MigrationError,
    create_backup,
    restore_backup,
    verify_backup,
)
from codex_hub.repository import Repository


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def create_sample_data(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    data_dir = tmp_path / "hub-data"
    settings = Settings(data_dir=data_dir)
    settings.ensure_directories()
    connection = connect_database(settings.database_path)
    initialize_schema(connection)
    repository = Repository(connection)
    owner = repository.create_owner_if_missing("owner", "correct horse battery staple")
    assert owner is not None
    device = repository.create_device("codex-device-a")
    project = repository.create_project(
        slug="portable-project",
        name="Portable Project",
        created_by_device_id=device["id"],
    )
    entry = repository.create_entry(
        project_id=project["id"],
        source_label="owner",
        kind="message",
        body_markdown="迁移后继续使用",
        author_user_id=owner["id"],
    )
    attachment_relative = "portable-project/continuity-资料.txt"
    attachment_bytes = "跨服务器保持完整。".encode("utf-8")
    attachment_path = settings.files_dir / attachment_relative
    attachment_path.parent.mkdir(parents=True)
    attachment_path.write_bytes(attachment_bytes)
    repository.create_attachment(
        project_id=project["id"],
        entry_id=entry["id"],
        original_filename="continuity-资料.txt",
        stored_relpath=attachment_relative,
        content_type="text/plain; charset=utf-8",
        byte_size=len(attachment_bytes),
        sha256=sha256_bytes(attachment_bytes),
        created_by_user_id=owner["id"],
    )
    release_bytes = b"portable-release"
    (settings.releases_dir / "CodexHub-test.apk").write_bytes(release_bytes)
    future_dir = data_dir / "future-state"
    future_dir.mkdir()
    (future_dir / "event-cursor.json").write_text(
        json.dumps({"cursor": 7}), encoding="utf-8"
    )
    connection.close()
    return data_dir, {
        "attachment_relative": attachment_relative,
        "attachment_bytes": attachment_bytes,
        "release_bytes": release_bytes,
    }


def test_portable_backup_round_trip_preserves_database_files_and_releases(
    tmp_path: Path,
):
    data_dir, sample = create_sample_data(tmp_path)
    archive = tmp_path / "portable.zip"

    created = create_backup(data_dir, archive)
    verified = verify_backup(archive)
    restored = restore_backup(archive, tmp_path / "restored-data")

    assert created["archive_sha256"] == verified["archive_sha256"]
    assert verified["verified"] is True
    assert verified["contains_sensitive_data"] is True
    assert verified["environment_file_included"] is False
    assert restored["restored"] is True
    restored_dir = Path(restored["target_data_dir"])
    assert (
        restored_dir / "files" / str(sample["attachment_relative"])
    ).read_bytes() == sample["attachment_bytes"]
    assert (
        restored_dir / "releases" / "CodexHub-test.apk"
    ).read_bytes() == sample["release_bytes"]
    assert json.loads(
        (restored_dir / "future-state" / "event-cursor.json").read_text(
            encoding="utf-8"
        )
    ) == {"cursor": 7}
    assert (restored_dir / ".codex-hub-restore.json").is_file()

    connection = sqlite3.connect(restored_dir / "codex_hub.db")
    try:
        assert connection.execute(
            "SELECT body_markdown FROM entries WHERE source_label = 'owner'"
        ).fetchone()[0] == "迁移后继续使用"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_online_backup_captures_committed_wal_without_copying_sidecars(tmp_path: Path):
    data_dir, _ = create_sample_data(tmp_path)
    database = data_dir / "codex_hub.db"
    connection = connect_database(database)
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute(
        "UPDATE projects SET description = ? WHERE slug = ?",
        ("committed in WAL", "portable-project"),
    )
    connection.commit()
    assert (data_dir / "codex_hub.db-wal").exists()

    archive = tmp_path / "live-wal.zip"
    create_backup(data_dir, archive)
    connection.close()

    with zipfile.ZipFile(archive) as backup:
        assert "payload/codex_hub.db" in backup.namelist()
        assert "payload/codex_hub.db-wal" not in backup.namelist()
        assert "payload/codex_hub.db-shm" not in backup.namelist()
    restored = restore_backup(archive, tmp_path / "wal-restored")
    restored_connection = sqlite3.connect(
        Path(restored["target_data_dir"]) / "codex_hub.db"
    )
    try:
        assert restored_connection.execute(
            "SELECT description FROM projects WHERE slug = 'portable-project'"
        ).fetchone()[0] == "committed in WAL"
    finally:
        restored_connection.close()


def test_verify_rejects_payload_tampering(tmp_path: Path):
    data_dir, _ = create_sample_data(tmp_path)
    archive = tmp_path / "portable.zip"
    tampered = tmp_path / "tampered.zip"
    create_backup(data_dir, archive)

    with zipfile.ZipFile(archive, "r") as source, zipfile.ZipFile(
        tampered, "w", compression=zipfile.ZIP_DEFLATED
    ) as target:
        for info in source.infolist():
            data = source.read(info)
            if info.filename.endswith("continuity-资料.txt"):
                data += b"tampered"
            target.writestr(info.filename, data)

    with pytest.raises(MigrationError, match="size does not match manifest"):
        verify_backup(tampered)


def test_verify_rejects_archive_path_traversal(tmp_path: Path):
    archive = tmp_path / "unsafe.zip"
    manifest = {
        "format": BACKUP_FORMAT,
        "format_version": BACKUP_FORMAT_VERSION,
        "files": [],
    }
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr(MANIFEST_NAME, json.dumps(manifest))
        target.writestr("../escape.txt", b"escape")

    with pytest.raises(MigrationError, match="Unsafe archive member"):
        verify_backup(archive)
    assert not (tmp_path / "escape.txt").exists()


def test_verify_rejects_case_collisions_for_cross_platform_restore(tmp_path: Path):
    archive = tmp_path / "case-collision.zip"
    payload = b"same"
    checksum = sha256_bytes(payload)
    paths = ["payload/files/Readme.txt", "payload/files/README.txt"]
    manifest = {
        "format": BACKUP_FORMAT,
        "format_version": BACKUP_FORMAT_VERSION,
        "directories": ["files"],
        "files": [
            {"path": path, "byte_size": len(payload), "sha256": checksum}
            for path in paths
        ],
    }
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr(MANIFEST_NAME, json.dumps(manifest))
        for path in paths:
            target.writestr(path, payload)

    with pytest.raises(MigrationError, match="case-insensitive systems"):
        verify_backup(archive)


def test_restore_refuses_an_existing_target(tmp_path: Path):
    data_dir, _ = create_sample_data(tmp_path)
    archive = tmp_path / "portable.zip"
    create_backup(data_dir, archive)
    target = tmp_path / "already-here"
    target.mkdir()

    with pytest.raises(MigrationError, match="already exists"):
        restore_backup(archive, target)


def test_create_refuses_database_attachment_mismatch(tmp_path: Path):
    data_dir, sample = create_sample_data(tmp_path)
    (data_dir / "files" / str(sample["attachment_relative"])).unlink()

    with pytest.raises(MigrationError, match="Attachment is missing"):
        create_backup(data_dir, tmp_path / "incomplete.zip")


def test_create_refuses_to_overwrite_an_existing_archive(tmp_path: Path):
    data_dir, _ = create_sample_data(tmp_path)
    archive = tmp_path / "portable.zip"
    archive.write_bytes(b"keep me")

    with pytest.raises(MigrationError, match="already exists"):
        create_backup(data_dir, archive)
    assert archive.read_bytes() == b"keep me"
