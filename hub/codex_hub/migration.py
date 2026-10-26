from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

from . import __version__


BACKUP_FORMAT = "codex-hub-portable-backup"
BACKUP_FORMAT_VERSION = 1
DATABASE_NAME = "codex_hub.db"
MANIFEST_NAME = "manifest.json"
PAYLOAD_PREFIX = "payload/"
RESTORE_RECORD_NAME = ".codex-hub-restore.json"
DEFAULT_MAX_EXTRACTED_BYTES = 100 * 1024 * 1024 * 1024
TRANSIENT_DATABASE_NAMES = {
    DATABASE_NAME,
    f"{DATABASE_NAME}-journal",
    f"{DATABASE_NAME}-shm",
    f"{DATABASE_NAME}-wal",
}
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
WINDOWS_RESERVED_CHARACTERS = set('<>:"|?*')


class MigrationError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_stream(source) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_size = 0
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        byte_size += len(chunk)
        digest.update(chunk)
    return digest.hexdigest(), byte_size


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise MigrationError(f"Cannot inspect path: {path}: {exc}") from exc
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return path.is_symlink() or bool(reparse_flag and attributes & reparse_flag)


def _assert_plain_directory(path: Path, *, label: str) -> None:
    if not path.exists():
        raise MigrationError(f"{label} does not exist: {path}")
    if _is_reparse_or_symlink(path):
        raise MigrationError(f"{label} cannot be a symbolic link or reparse point: {path}")
    if not path.is_dir():
        raise MigrationError(f"{label} is not a directory: {path}")


def _safe_archive_name(name: str) -> PurePosixPath:
    if not name or "\x00" in name or "\\" in name or name.startswith("/"):
        raise MigrationError(f"Unsafe archive member: {name!r}")
    value = PurePosixPath(name)
    if any(part in {"", ".", ".."} for part in value.parts):
        raise MigrationError(f"Unsafe archive member: {name!r}")
    for part in value.parts:
        stem = part.split(".", 1)[0].upper()
        if (
            any(character in WINDOWS_RESERVED_CHARACTERS for character in part)
            or part.endswith((" ", "."))
            or stem in WINDOWS_RESERVED_NAMES
        ):
            raise MigrationError(f"Archive member is not cross-platform safe: {name!r}")
    return value


def _safe_attachment_path(value: str) -> PurePosixPath:
    normalized = str(value or "").replace("\\", "/")
    path = _safe_archive_name(normalized)
    if path.parts and path.parts[0].endswith(":"):
        raise MigrationError(f"Unsafe attachment path: {value!r}")
    return path


def _archive_info_is_link(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def _iter_plain_files(
    root: Path,
    *,
    exclude_live_database_files: bool = False,
) -> Iterable[tuple[Path, PurePosixPath]]:
    def walk(directory: Path, relative: PurePosixPath) -> Iterable[tuple[Path, PurePosixPath]]:
        try:
            with os.scandir(directory) as scanner:
                entries = sorted(scanner, key=lambda item: item.name.casefold())
        except OSError as exc:
            raise MigrationError(f"Cannot read data directory: {directory}: {exc}") from exc
        for entry in entries:
            source = Path(entry.path)
            child_relative = relative / entry.name
            if _is_reparse_or_symlink(source):
                raise MigrationError(
                    f"Data directory contains a symbolic link or reparse point: {source}"
                )
            if entry.is_dir(follow_symlinks=False):
                yield from walk(source, child_relative)
            elif entry.is_file(follow_symlinks=False):
                if (
                    exclude_live_database_files
                    and len(child_relative.parts) == 1
                    and entry.name in TRANSIENT_DATABASE_NAMES
                ):
                    continue
                if len(child_relative.parts) == 1 and entry.name == RESTORE_RECORD_NAME:
                    continue
                yield source, child_relative
            else:
                raise MigrationError(f"Unsupported data entry type: {source}")

    yield from walk(root, PurePosixPath())


def _online_database_backup(source_path: Path, target_path: Path) -> None:
    if not source_path.is_file() or _is_reparse_or_symlink(source_path):
        raise MigrationError(f"SQLite database is missing or unsafe: {source_path}")
    source_uri = f"{source_path.resolve().as_uri()}?mode=ro"
    try:
        source = sqlite3.connect(source_uri, uri=True, timeout=30)
        target = sqlite3.connect(target_path)
        try:
            with target:
                source.backup(target)
        finally:
            target.close()
            source.close()
    except sqlite3.Error as exc:
        raise MigrationError(f"SQLite online backup failed: {exc}") from exc


def _database_summary(
    database_path: Path,
    payload_records: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    database_uri = f"{database_path.resolve().as_uri()}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(database_uri, uri=True, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
            if integrity != ["ok"]:
                raise MigrationError(
                    "SQLite integrity check failed: " + "; ".join(integrity[:10])
                )
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_keys:
                raise MigrationError(
                    f"SQLite foreign-key check found {len(foreign_keys)} violation(s)"
                )
            table_names = [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                    ORDER BY name
                    """
                )
            ]
            table_counts: dict[str, int] = {}
            for table_name in table_names:
                quoted = '"' + table_name.replace('"', '""') + '"'
                table_counts[table_name] = int(
                    connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
                )
            attachment_count = 0
            if "attachments" in table_names:
                attachments = connection.execute(
                    "SELECT stored_relpath, byte_size, sha256 FROM attachments ORDER BY id"
                ).fetchall()
                attachment_count = len(attachments)
                for attachment in attachments:
                    relative = _safe_attachment_path(str(attachment["stored_relpath"]))
                    archive_name = PAYLOAD_PREFIX + "files/" + relative.as_posix()
                    record = payload_records.get(archive_name)
                    if record is None:
                        raise MigrationError(
                            f"Attachment is missing from backup payload: {relative.as_posix()}"
                        )
                    expected_size = int(attachment["byte_size"])
                    expected_sha256 = str(attachment["sha256"] or "").casefold()
                    if int(record["byte_size"]) != expected_size:
                        raise MigrationError(
                            f"Attachment size does not match database: {relative.as_posix()}"
                        )
                    if str(record["sha256"]).casefold() != expected_sha256:
                        raise MigrationError(
                            f"Attachment checksum does not match database: {relative.as_posix()}"
                        )
            return {
                "integrity_check": "ok",
                "foreign_key_violations": 0,
                "table_counts": table_counts,
                "attachment_count": attachment_count,
            }
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise MigrationError(f"Cannot validate SQLite database: {exc}") from exc


def _payload_records(payload_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for source, relative in _iter_plain_files(payload_root):
        archive_path = PAYLOAD_PREFIX + relative.as_posix()
        _safe_archive_name(archive_path)
        records.append(
            {
                "path": archive_path,
                "byte_size": source.stat().st_size,
                "sha256": sha256_file(source),
            }
        )
    return sorted(records, key=lambda item: str(item["path"]).casefold())


def create_backup(data_dir: Path, output_path: Path) -> dict[str, Any]:
    source_root = data_dir.expanduser().absolute()
    _assert_plain_directory(source_root, label="Data directory")
    source_root = source_root.resolve(strict=True)
    database_path = source_root / DATABASE_NAME

    output = output_path.expanduser().absolute()
    output_parent = output.parent
    output_parent.mkdir(parents=True, exist_ok=True)
    output_parent = output_parent.resolve(strict=True)
    output = output_parent / output.name
    if output.exists() or output.is_symlink():
        raise MigrationError(f"Backup output already exists: {output}")
    if output_parent == source_root or output_parent.is_relative_to(source_root):
        raise MigrationError("Backup output must be outside the Hub data directory")

    with tempfile.TemporaryDirectory(prefix="codex-hub-backup-", dir=output_parent) as temp_name:
        staging = Path(temp_name)
        payload = staging / "payload"
        payload.mkdir()
        _online_database_backup(database_path, payload / DATABASE_NAME)
        for source, relative in _iter_plain_files(
            source_root, exclude_live_database_files=True
        ):
            destination = payload.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copyfile(source, destination)
            except OSError as exc:
                raise MigrationError(f"Cannot copy data file {source}: {exc}") from exc

        for required_directory in (payload / "files", payload / "releases"):
            required_directory.mkdir(parents=True, exist_ok=True)

        records = _payload_records(payload)
        record_map = {str(item["path"]): item for item in records}
        database = _database_summary(payload / DATABASE_NAME, record_map)
        directory_names = sorted(
            {
                path.relative_to(payload).as_posix()
                for path in payload.rglob("*")
                if path.is_dir()
            },
            key=str.casefold,
        )
        for directory_name in directory_names:
            _safe_archive_name(directory_name)
        manifest: dict[str, Any] = {
            "format": BACKUP_FORMAT,
            "format_version": BACKUP_FORMAT_VERSION,
            "created_at": utc_now(),
            "application_version": __version__,
            "contains_sensitive_data": True,
            "environment_file_included": False,
            "database": database,
            "directories": directory_names,
            "files": records,
        }
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")

        temporary_output = output_parent / f".{output.name}.{uuid.uuid4().hex}.tmp"
        try:
            with zipfile.ZipFile(
                temporary_output,
                mode="x",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
                allowZip64=True,
            ) as archive:
                archive.writestr(MANIFEST_NAME, manifest_bytes)
                for record in records:
                    name = str(record["path"])
                    relative = PurePosixPath(name).relative_to("payload")
                    archive.write(payload.joinpath(*relative.parts), arcname=name)
            os.replace(temporary_output, output)
        finally:
            if temporary_output.exists():
                temporary_output.unlink()

    return {
        "archive": str(output),
        "archive_sha256": sha256_file(output),
        "archive_bytes": output.stat().st_size,
        "application_version": manifest["application_version"],
        "database": manifest["database"],
        "file_count": len(manifest["files"]),
        "contains_sensitive_data": True,
        "environment_file_included": False,
    }


def _read_and_validate_manifest(
    archive: zipfile.ZipFile,
    *,
    max_extracted_bytes: int,
) -> tuple[dict[str, Any], dict[str, zipfile.ZipInfo]]:
    infos: dict[str, zipfile.ZipInfo] = {}
    for info in archive.infolist():
        name = str(_safe_archive_name(info.filename))
        if name in infos:
            raise MigrationError(f"Archive contains duplicate member: {name}")
        if info.is_dir() or _archive_info_is_link(info):
            raise MigrationError(f"Archive contains unsupported member: {name}")
        infos[name] = info
    if MANIFEST_NAME not in infos:
        raise MigrationError("Backup manifest is missing")
    manifest_info = infos[MANIFEST_NAME]
    if manifest_info.file_size > 4 * 1024 * 1024:
        raise MigrationError("Backup manifest is unexpectedly large")
    try:
        manifest = json.loads(archive.read(manifest_info).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"Backup manifest is invalid UTF-8 JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise MigrationError("Backup manifest must be a JSON object")
    if manifest.get("format") != BACKUP_FORMAT:
        raise MigrationError("Unsupported backup format")
    if manifest.get("format_version") != BACKUP_FORMAT_VERSION:
        raise MigrationError(
            f"Unsupported backup format version: {manifest.get('format_version')!r}"
        )
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        raise MigrationError("Backup manifest files must be a list")
    raw_directories = manifest.get("directories")
    if not isinstance(raw_directories, list):
        raise MigrationError("Backup manifest directories must be a list")
    normalized_directories: list[str] = []
    casefold_directories: dict[str, str] = {}
    for raw_directory in raw_directories:
        directory_name = str(_safe_archive_name(str(raw_directory)))
        folded_directory = directory_name.casefold()
        if folded_directory in casefold_directories:
            raise MigrationError(
                "Backup contains directories that collide on case-insensitive systems: "
                f"{casefold_directories[folded_directory]!r}, {directory_name!r}"
            )
        casefold_directories[folded_directory] = directory_name
        normalized_directories.append(directory_name)
    manifest["directories"] = sorted(normalized_directories, key=str.casefold)
    manifest_records: dict[str, dict[str, Any]] = {}
    casefold_paths: dict[str, str] = {}
    total_bytes = 0
    for raw_record in raw_files:
        if not isinstance(raw_record, dict):
            raise MigrationError("Backup manifest contains an invalid file record")
        name = str(_safe_archive_name(str(raw_record.get("path") or "")))
        if not name.startswith(PAYLOAD_PREFIX):
            raise MigrationError(f"Backup payload path is outside payload/: {name}")
        if name in manifest_records:
            raise MigrationError(f"Backup manifest contains duplicate path: {name}")
        folded_name = name.casefold()
        if folded_name in casefold_paths:
            raise MigrationError(
                "Backup contains paths that collide on case-insensitive systems: "
                f"{casefold_paths[folded_name]!r}, {name!r}"
            )
        casefold_paths[folded_name] = name
        try:
            byte_size = int(raw_record["byte_size"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MigrationError(f"Invalid byte size for backup member: {name}") from exc
        sha256 = str(raw_record.get("sha256") or "").casefold()
        if byte_size < 0 or len(sha256) != 64 or any(
            character not in "0123456789abcdef" for character in sha256
        ):
            raise MigrationError(f"Invalid checksum record for backup member: {name}")
        total_bytes += byte_size
        manifest_records[name] = {
            "path": name,
            "byte_size": byte_size,
            "sha256": sha256,
        }
    if total_bytes > max_extracted_bytes:
        raise MigrationError(
            f"Backup expands to {total_bytes} bytes, above the configured safety limit"
        )
    payload_infos = {
        name: info for name, info in infos.items() if name.startswith(PAYLOAD_PREFIX)
    }
    if set(payload_infos) != set(manifest_records):
        missing = sorted(set(manifest_records) - set(payload_infos))
        unexpected = sorted(set(payload_infos) - set(manifest_records))
        raise MigrationError(
            f"Backup payload does not match manifest; missing={missing[:5]}, "
            f"unexpected={unexpected[:5]}"
        )
    if set(infos) != {MANIFEST_NAME, *payload_infos}:
        unexpected = sorted(set(infos) - {MANIFEST_NAME, *payload_infos})
        raise MigrationError(f"Archive contains unexpected members: {unexpected[:5]}")
    for name, info in payload_infos.items():
        record = manifest_records[name]
        if info.file_size != record["byte_size"]:
            raise MigrationError(f"Archive member size does not match manifest: {name}")
    manifest["files"] = list(manifest_records.values())
    return manifest, payload_infos


def verify_backup(
    archive_path: Path,
    *,
    max_extracted_bytes: int = DEFAULT_MAX_EXTRACTED_BYTES,
) -> dict[str, Any]:
    source_input = archive_path.expanduser().absolute()
    if not source_input.exists() or _is_reparse_or_symlink(source_input):
        raise MigrationError(f"Backup archive is missing or unsafe: {source_input}")
    source = source_input.resolve(strict=True)
    if not source.is_file():
        raise MigrationError(f"Backup archive is not a file: {source}")
    try:
        with zipfile.ZipFile(source, mode="r", allowZip64=True) as archive:
            manifest, payload_infos = _read_and_validate_manifest(
                archive, max_extracted_bytes=max_extracted_bytes
            )
            records = {str(item["path"]): item for item in manifest["files"]}
            for name, info in payload_infos.items():
                with archive.open(info, mode="r") as member:
                    checksum, byte_size = _sha256_stream(member)
                record = records[name]
                if byte_size != int(record["byte_size"]):
                    raise MigrationError(f"Backup member size check failed: {name}")
                if checksum != str(record["sha256"]):
                    raise MigrationError(f"Backup member checksum failed: {name}")
            database_info = payload_infos.get(PAYLOAD_PREFIX + DATABASE_NAME)
            if database_info is None:
                raise MigrationError("Backup does not contain the Hub database")
            with tempfile.TemporaryDirectory(prefix="codex-hub-verify-") as temp_name:
                database_path = Path(temp_name) / DATABASE_NAME
                with archive.open(database_info, mode="r") as source_db, database_path.open(
                    "xb"
                ) as target_db:
                    shutil.copyfileobj(source_db, target_db, 1024 * 1024)
                database = _database_summary(database_path, records)
            if database != manifest.get("database"):
                raise MigrationError("Database summary does not match backup manifest")
    except zipfile.BadZipFile as exc:
        raise MigrationError(f"Backup is not a valid ZIP archive: {exc}") from exc
    return {
        "archive": str(source),
        "archive_sha256": sha256_file(source),
        "archive_bytes": source.stat().st_size,
        "application_version": manifest.get("application_version"),
        "created_at": manifest.get("created_at"),
        "database": manifest["database"],
        "file_count": len(manifest["files"]),
        "contains_sensitive_data": bool(manifest.get("contains_sensitive_data")),
        "environment_file_included": bool(
            manifest.get("environment_file_included")
        ),
        "verified": True,
    }


def restore_backup(
    archive_path: Path,
    target_data_dir: Path,
    *,
    max_extracted_bytes: int = DEFAULT_MAX_EXTRACTED_BYTES,
) -> dict[str, Any]:
    source_input = archive_path.expanduser().absolute()
    if not source_input.exists() or _is_reparse_or_symlink(source_input):
        raise MigrationError(f"Backup archive is missing or unsafe: {source_input}")
    source = source_input.resolve(strict=True)
    target = target_data_dir.expanduser().absolute()
    if target.exists() or target.is_symlink():
        raise MigrationError(
            f"Restore target already exists; restore only into a new staging path: {target}"
        )
    target_parent = target.parent
    target_parent.mkdir(parents=True, exist_ok=True)
    _assert_plain_directory(target_parent, label="Restore parent")
    target_parent = target_parent.resolve(strict=True)
    target = target_parent / target.name
    if target == target_parent:
        raise MigrationError("Restore target must be a new child directory")

    verification = verify_backup(source, max_extracted_bytes=max_extracted_bytes)
    staging = target_parent / f".{target.name}.restore-{uuid.uuid4().hex}"
    if staging.exists():
        raise MigrationError(f"Unexpected restore staging collision: {staging}")
    staging.mkdir(mode=0o750)
    email_quarantine: dict[str, int] = {}
    mail_quarantine: dict[str, int] = {}
    try:
        with zipfile.ZipFile(source, mode="r", allowZip64=True) as archive:
            manifest, payload_infos = _read_and_validate_manifest(
                archive, max_extracted_bytes=max_extracted_bytes
            )
            directories = manifest.get("directories") or []
            if not isinstance(directories, list):
                raise MigrationError("Backup manifest directories must be a list")
            for raw_directory in directories:
                relative = _safe_archive_name(str(raw_directory))
                destination = staging.joinpath(*relative.parts)
                destination.mkdir(parents=True, exist_ok=True)
            for name in sorted(payload_infos, key=str.casefold):
                relative = PurePosixPath(name).relative_to("payload")
                destination = staging.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(payload_infos[name], mode="r") as source_file, destination.open(
                    "xb"
                ) as target_file:
                    shutil.copyfileobj(source_file, target_file, 1024 * 1024)
            record_path = staging / RESTORE_RECORD_NAME
            record = {
                "restored_at": utc_now(),
                "source_archive": source.name,
                "source_archive_sha256": verification["archive_sha256"],
                "backup_manifest": manifest,
            }
            record_path.write_text(
                json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            restored_records = _payload_records(staging)
            restored_map = {str(item["path"]): item for item in restored_records}
            expected_map = {str(item["path"]): item for item in manifest["files"]}
            if restored_map != expected_map:
                raise MigrationError("Restored files do not match the backup manifest")
            database = _database_summary(staging / DATABASE_NAME, restored_map)
            if database != manifest.get("database"):
                raise MigrationError("Restored database does not match the backup manifest")
        # Verify the archived bytes first, then quarantine outbound work. A backup
        # cannot tell us which emails were accepted after that snapshot was made.
        connection = sqlite3.connect(staging / DATABASE_NAME, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='email_deliveries'"
            ).fetchone():
                from .notifications.storage import NotificationStore

                email_quarantine = NotificationStore(connection).quarantine_restored_queue()
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mail_deliveries'"
            ).fetchone():
                from .mail.storage import MailStore

                mail_quarantine = MailStore(connection).quarantine_restored_queue()
        finally:
            connection.close()
        record["outbound_email"] = {
            "status": "disabled_pending_review",
            "quarantine": email_quarantine,
            "mail_quarantine": mail_quarantine,
        }
        record_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if target.exists() or target.is_symlink():
            raise MigrationError(f"Restore target appeared during restore: {target}")
        os.replace(staging, target)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    return {
        **verification,
        "target_data_dir": str(target),
        "restore_record": str(target / RESTORE_RECORD_NAME),
        "restored": True,
        "email_notifications_disabled": True,
        "email_quarantine": email_quarantine,
        "mail_quarantine": mail_quarantine,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create, verify, or stage a portable Codex Hub server-data backup."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="Create a verified portable backup")
    create.add_argument("--data-dir", required=True, type=Path)
    create.add_argument("--output", required=True, type=Path)

    verify = subparsers.add_parser("verify", help="Verify an existing portable backup")
    verify.add_argument("--archive", required=True, type=Path)
    verify.add_argument(
        "--max-extracted-bytes",
        type=int,
        default=DEFAULT_MAX_EXTRACTED_BYTES,
    )

    restore = subparsers.add_parser(
        "restore", help="Restore into a new staging data directory"
    )
    restore.add_argument("--archive", required=True, type=Path)
    restore.add_argument("--target-data-dir", required=True, type=Path)
    restore.add_argument(
        "--max-extracted-bytes",
        type=int,
        default=DEFAULT_MAX_EXTRACTED_BYTES,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "create":
            result = create_backup(arguments.data_dir, arguments.output)
        elif arguments.command == "verify":
            result = verify_backup(
                arguments.archive,
                max_extracted_bytes=arguments.max_extracted_bytes,
            )
        else:
            result = restore_backup(
                arguments.archive,
                arguments.target_data_dir,
                max_extracted_bytes=arguments.max_extracted_bytes,
            )
    except (MigrationError, OSError) as exc:
        print(f"codex-hub-migrate: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
