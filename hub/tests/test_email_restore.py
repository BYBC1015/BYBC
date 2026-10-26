import json
import sqlite3
from pathlib import Path

from codex_hub.config import Settings
from codex_hub.db import connect_database, initialize_schema
from codex_hub.migration import create_backup, restore_backup
from codex_hub.notifications.storage import NotificationStore, utc_now
from codex_hub.repository import Repository


def test_restore_disables_notifications_and_quarantines_old_queue(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "source")
    settings.ensure_directories()
    connection = connect_database(settings.database_path)
    initialize_schema(connection)
    repo = Repository(connection)
    owner = repo.create_owner_if_missing("owner", "test-password")
    store = NotificationStore(connection)
    verification = store.request_verification(owner["id"], "123456@qq.com", token_secret="test-secret")
    assert store.confirm_verification(verification["token"])
    store.update_settings(owner["id"], enabled=True)
    queued = store.enqueue_test(owner["id"])
    # One unresolved request and one definitely unsubmitted item are in backup.
    connection.execute(
        "UPDATE email_deliveries SET status='sending',first_attempt_at=?,lease_token='old' WHERE id=?",
        (utc_now(), verification["delivery_id"]),
    )
    connection.commit()
    archive = tmp_path / "snapshot.zip"
    create_backup(settings.data_dir, archive)
    connection.close()

    result = restore_backup(archive, tmp_path / "restored")
    assert result["email_notifications_disabled"] is True
    assert result["email_quarantine"] == {"owners_disabled": 1, "cancelled": 1, "uncertain": 1}
    restored = sqlite3.connect(tmp_path / "restored" / "codex_hub.db")
    restored.row_factory = sqlite3.Row
    try:
        row = restored.execute("SELECT * FROM email_notification_settings").fetchone()
        assert row["enabled"] == 0 and row["enabled_at"] is None
        states = {row["id"]: row["status"] for row in restored.execute("SELECT * FROM email_deliveries")}
        assert states[queued["id"]] == "cancelled"
        assert states[verification["delivery_id"]] == "uncertain"
        assert restored.execute("SELECT paused_reason FROM email_channel_state").fetchone()[0] == "backup_restored"
        assert restored.execute("SELECT COUNT(*) FROM email_deliveries WHERE lease_token IS NOT NULL").fetchone()[0] == 0
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        restored.close()
    record = json.loads((tmp_path / "restored" / ".codex-hub-restore.json").read_text(encoding="utf-8"))
    assert record["outbound_email"]["status"] == "disabled_pending_review"
