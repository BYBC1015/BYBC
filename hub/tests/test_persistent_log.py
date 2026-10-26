from codex_bridge.persistent_log import PersistentLog


def test_persistent_log_survives_restart_and_rotates(tmp_path):
    path = tmp_path / "bridge.log"
    log = PersistentLog(path, max_bytes=1024)

    log.write("first event")
    assert any("first event" in line for line in PersistentLog(path).tail())

    for index in range(80):
        log.write(f"event {index}: " + "x" * 40)

    assert path.exists()
    assert path.with_suffix(".log.1").exists()
    assert len(log.tail(10)) <= 10

    log.clear()
    assert log.tail() == []
    assert not path.with_suffix(".log.1").exists()
