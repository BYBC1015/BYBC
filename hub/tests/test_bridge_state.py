import json

import pytest

from codex_bridge.state import BridgeState


@pytest.mark.parametrize(
    "payload",
    [
        b"\x00" * 128,
        b'{"initialized": tru',
        b"\xff\xfe\xfd",
        b"[]",
    ],
    ids=["nul-filled", "truncated-json", "invalid-utf8", "non-object-json"],
)
def test_load_preserves_corrupt_state_and_allows_valid_save(tmp_path, payload):
    path = tmp_path / "state.json"
    path.write_bytes(payload)

    state = BridgeState.load(path)

    assert state == BridgeState()
    backups = list(tmp_path.glob("state.json.corrupt-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == payload

    state.initialized = True
    state.save(path)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["initialized"] is True


def test_load_does_not_overwrite_an_existing_corrupt_backup(tmp_path):
    path = tmp_path / "state.json"
    payload = b'{"initialized":'
    path.write_bytes(payload)
    existing = tmp_path / "state.json.corrupt-existing.bak"
    existing.write_bytes(b"older corrupt state")

    assert BridgeState.load(path) == BridgeState()

    backups = list(tmp_path.glob("state.json.corrupt-*.bak"))
    assert len(backups) == 2
    assert existing.read_bytes() == b"older corrupt state"
    assert any(item.read_bytes() == payload for item in backups if item != existing)


def test_load_accepts_valid_utf8_bom_state(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        '{"initialized": true, "seen_task_ids": [42]}',
        encoding="utf-8-sig",
    )

    state = BridgeState.load(path)

    assert state.initialized is True
    assert state.seen_task_ids == {"42"}
    assert list(tmp_path.glob("state.json.corrupt-*.bak")) == []


def test_load_safely_ignores_scalar_set_fields(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        '{"initialized": true, "seen_task_ids": 42, "seen_mention_ids": "7"}',
        encoding="utf-8",
    )

    state = BridgeState.load(path)

    assert state.initialized is True
    assert state.seen_task_ids == set()
    assert state.seen_mention_ids == set()
