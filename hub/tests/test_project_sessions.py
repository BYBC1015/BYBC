import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from codex_bridge.project_sessions import (
    ProjectSessionBusy,
    ProjectSessionError,
    ProjectSessionMismatch,
    ProjectSessionRecoveryRequired,
    ProjectSessionStore,
)


THREAD = "01a18355-7a8c-74e0-a3a1-f599a91f94d3"


@pytest.fixture
def scope(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    return {"device_id": "BYBC-YOLO", "project_slug": "hub", "conversation_id": 71,
            "assigned_agent_id": 6, "project_path": project, "run_key": "run-one", "generation": 2}


def events(thread=THREAD, completed=True):
    stream = json.dumps({"type": "thread.started", "thread_id": thread}) + "\n"
    if completed:
        stream += json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}})
    return stream


def finish(lease, thread=THREAD, message="saved"):
    lease.start()
    lease.observe_jsonl(events(thread))
    return lease.complete(ok=True, returncode=0, last_message=message)


def test_new_then_explicit_resume_persists_same_thread(tmp_path, scope):
    root = tmp_path / "sessions"
    with ProjectSessionStore(root).acquire(**scope) as first:
        assert first.session_id is None
        receipt = finish(first)
    assert receipt["identity"]["assigned_agent_id"] == "6"
    assert receipt["generation"] == "2"
    with ProjectSessionStore(root).acquire(**{**scope, "run_key": "run-two"}) as second:
        assert second.session_id == THREAD
        assert second.project_path == scope["project_path"]
        finish(second, message="resumed with context")
    assert len(list((root / "bindings").glob("*.json"))) == 1


def test_completed_retry_uses_receipt_and_cannot_execute_again(tmp_path, scope):
    store = ProjectSessionStore(tmp_path / "sessions")
    with store.acquire(**scope) as lease:
        receipt = finish(lease)
    with store.acquire(**scope) as duplicate:
        assert duplicate.cached_receipt == receipt
        with pytest.raises(ProjectSessionError, match="完成收据"):
            duplicate.start()


@pytest.mark.parametrize("field,value", [
    ("assigned_agent_id", 8), ("conversation_id", 72),
    ("device_id", "BYBC-PRO"), ("project_slug", "another-project"),
])
def test_identities_have_independent_threads(tmp_path, scope, field, value):
    store = ProjectSessionStore(tmp_path / "sessions")
    with store.acquire(**scope) as lease:
        finish(lease)
    with store.acquire(**{**scope, field: value}) as separate:
        assert separate.session_id is None
        assert separate.cached_receipt is None
        finish(separate, str(uuid.uuid4()))


def test_normalized_device_and_project_use_same_binding(tmp_path, scope):
    store = ProjectSessionStore(tmp_path / "sessions")
    with store.acquire(**scope) as lease:
        finish(lease)
    with store.acquire(**{**scope, "device_id": " bybc-yolo ", "project_slug": "HUB"}) as lease:
        assert lease.session_id == THREAD
        assert lease.cached_receipt["ok"] is True


def test_workspace_change_is_not_a_new_binding(tmp_path, scope):
    store = ProjectSessionStore(tmp_path / "sessions")
    other = tmp_path / "other-project"
    other.mkdir()
    with store.acquire(**scope) as lease:
        finish(lease)
    with pytest.raises(ProjectSessionMismatch, match="其他目录"):
        with store.acquire(**{**scope, "project_path": other}):
            pytest.fail("different workspace must not execute")


@pytest.mark.parametrize("invalid", ["relative/project", "", None])
def test_invalid_project_path_is_rejected(tmp_path, scope, invalid):
    store = ProjectSessionStore(tmp_path / "sessions")
    with pytest.raises(ProjectSessionMismatch):
        with store.acquire(**{**scope, "project_path": invalid}):
            pytest.fail("invalid workspace must not execute")


def test_same_conversation_is_exclusive_but_other_agent_can_run(tmp_path, scope):
    store = ProjectSessionStore(tmp_path / "sessions")
    with store.acquire(**scope):
        with pytest.raises(ProjectSessionBusy):
            with ProjectSessionStore(store.root).acquire(**scope):
                pytest.fail("same thread cannot have concurrent writers")
        with store.acquire(**{**scope, "assigned_agent_id": 9}) as other:
            finish(other, str(uuid.uuid4()))


def test_cross_process_lock_does_not_allow_second_writer(tmp_path, scope):
    store = ProjectSessionStore(tmp_path / "sessions")
    args = {**scope, "project_path": str(scope["project_path"])}
    script = (
        "import json,sys; from codex_bridge.project_sessions import ProjectSessionStore,ProjectSessionBusy\n"
        "try:\n"
        " with ProjectSessionStore(sys.argv[1]).acquire(**json.loads(sys.argv[2])): sys.exit(8)\n"
        "except ProjectSessionBusy: sys.exit(0)\n"
    )
    with store.acquire(**scope):
        reply = subprocess.run([sys.executable, "-c", script, str(store.root), json.dumps(args)],
                               cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15)
    assert reply.returncode == 0, reply.stderr.decode(errors="replace")


def test_known_uuid_cannot_be_reused_by_another_identity(tmp_path, scope):
    store = ProjectSessionStore(tmp_path / "sessions")
    with store.acquire(**scope) as lease:
        finish(lease)
    with store.acquire(**{**scope, "assigned_agent_id": 9}) as other:
        other.start()
        with pytest.raises(ProjectSessionMismatch, match="另一个智能体"):
            other.observe_jsonl(events())


def test_resume_returning_new_thread_is_rejected_and_original_retained(tmp_path, scope):
    store = ProjectSessionStore(tmp_path / "sessions")
    with store.acquire(**scope) as lease:
        finish(lease)
    with store.acquire(**{**scope, "run_key": "run-two"}) as lease:
        lease.start()
        with pytest.raises(ProjectSessionMismatch, match="另一个对话"):
            lease.observe_jsonl(events(str(uuid.uuid4())))
        assert lease.session_id == THREAD
        with pytest.raises(ProjectSessionRecoveryRequired):
            lease.complete(ok=True, returncode=0)
    with pytest.raises(ProjectSessionRecoveryRequired):
        with store.acquire(**{**scope, "run_key": "run-three"}):
            pytest.fail("must not replace broken conversation")


@pytest.mark.parametrize("with_thread", [False, True])
def test_interrupted_turn_never_restarts_automatically(tmp_path, scope, with_thread):
    store = ProjectSessionStore(tmp_path / "sessions")
    with store.acquire(**scope) as lease:
        lease.start()
        if with_thread:
            lease.observe_jsonl(events(completed=False))
    with pytest.raises(ProjectSessionRecoveryRequired, match="尚未确认"):
        with store.acquire(**{**scope, "run_key": "next"}):
            pytest.fail("unknown previous result must not duplicate work")


def test_crash_before_start_leaves_reusable_empty_binding(tmp_path, scope):
    store = ProjectSessionStore(tmp_path / "sessions")
    with store.acquire(**scope):
        pass
    with store.acquire(**scope) as lease:
        assert lease.session_id is None
        assert lease.started is False
        finish(lease)
        assert lease.started is True


def test_receipt_recovers_crash_between_receipt_and_binding_update(tmp_path, scope, monkeypatch):
    import codex_bridge.project_sessions as module
    store = ProjectSessionStore(tmp_path / "sessions")
    original_write = module._write
    with store.acquire(**scope) as lease:
        lease.start()
        lease.observe_jsonl(events())

        def fail_binding_commit(path, record):
            if path.parent.name == "bindings" and record.get("pending_run") is None:
                raise OSError("simulated power loss after receipt")
            return original_write(path, record)

        monkeypatch.setattr(module, "_write", fail_binding_commit)
        with pytest.raises(OSError):
            lease.complete(ok=True, returncode=0, last_message="already finished")
    monkeypatch.setattr(module, "_write", original_write)
    with store.acquire(**scope) as recovered:
        assert recovered.cached_receipt["last_message"] == "already finished"
    with store.acquire(**{**scope, "run_key": "next"}) as next_turn:
        assert next_turn.session_id == THREAD
        finish(next_turn)


@pytest.mark.parametrize("malformation", ["invalid-json", "missing-session", "invalid-pending"])
def test_corrupt_binding_is_never_silently_reset(tmp_path, scope, malformation):
    store = ProjectSessionStore(tmp_path / "sessions")
    with store.acquire(**scope):
        pass
    path = next((store.root / "bindings").glob("*.json"))
    record = json.loads(path.read_text(encoding="utf-8"))
    if malformation == "invalid-json":
        path.write_text("{broken", encoding="utf-8")
    elif malformation == "missing-session":
        record.pop("session_id")
        path.write_text(json.dumps(record), encoding="utf-8")
    else:
        record["pending_run"] = {}
        path.write_text(json.dumps(record), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ProjectSessionRecoveryRequired):
        with store.acquire(**scope):
            pytest.fail("corrupt binding must fail closed")
    assert path.read_bytes() == before


def test_corrupt_receipt_is_not_treated_as_cache_miss(tmp_path, scope):
    store = ProjectSessionStore(tmp_path / "sessions")
    with store.acquire(**scope) as lease:
        finish(lease)
        receipt_path = lease.receipt_path
    receipt_path.write_text("[]", encoding="utf-8")
    with pytest.raises(ProjectSessionRecoveryRequired):
        with store.acquire(**scope):
            pytest.fail("bad receipt must not cause duplicate execution")


@pytest.mark.parametrize("stream", [events(completed=False), '{"type":"turn.completed"}',
                                   events() + '\n{"type":"turn.failed"}'])
def test_incomplete_event_stream_cannot_be_marked_successful(tmp_path, scope, stream):
    store = ProjectSessionStore(tmp_path / "sessions")
    with store.acquire(**scope) as lease:
        lease.start()
        lease.observe_jsonl(stream)
        with pytest.raises(ProjectSessionRecoveryRequired):
            lease.complete(ok=True, returncode=0)


def test_resume_failure_preserves_explicit_thread_for_next_attempt(tmp_path, scope):
    store = ProjectSessionStore(tmp_path / "sessions")
    with store.acquire(**scope) as lease:
        finish(lease)
    with store.acquire(**{**scope, "run_key": "failed"}) as lease:
        lease.start()
        lease.observe_jsonl('{"type":"error","message":"session unavailable"}')
        receipt = lease.complete(ok=False, returncode=1, error="session unavailable")
        assert receipt["session_id"] == THREAD
    with store.acquire(**{**scope, "run_key": "retry"}) as lease:
        assert lease.session_id == THREAD  # never falls back to a new exec


def test_unconfirmed_create_failure_cannot_silently_create_another_thread(tmp_path, scope):
    store = ProjectSessionStore(tmp_path / "sessions")
    with store.acquire(**scope) as lease:
        lease.start()
        with pytest.raises(ProjectSessionRecoveryRequired, match="未确认"):
            lease.complete(ok=False, returncode=1, error="connection lost")
    with pytest.raises(ProjectSessionRecoveryRequired):
        with store.acquire(**scope):
            pytest.fail("new session may already exist")


def test_unknown_process_result_preserves_pending(tmp_path, scope):
    store = ProjectSessionStore(tmp_path / "sessions")
    with store.acquire(**scope) as lease:
        lease.start()
        lease.observe_jsonl(events(completed=False))
        with pytest.raises(ProjectSessionRecoveryRequired, match="结果不明确"):
            lease.complete(ok=False, returncode=None)


def test_jsonl_noise_and_duplicate_event_are_tolerated(tmp_path, scope):
    store = ProjectSessionStore(tmp_path / "sessions")
    with store.acquire(**scope) as lease:
        lease.start()
        lease.observe_jsonl("diagnostic line\nnull\n[]\n" + events() + "\n" + events())
        assert lease.complete(ok=True, returncode=0)["session_id"] == THREAD


def test_methods_cannot_write_after_lock_has_been_released(tmp_path, scope):
    store = ProjectSessionStore(tmp_path / "sessions")
    with store.acquire(**scope) as lease:
        pass
    with pytest.raises(ProjectSessionError, match="锁已释放"):
        lease.start()


@pytest.mark.parametrize("existing", [False, True])
def test_proven_unstarted_abort_releases_pending_without_result_or_lost_thread(tmp_path, scope, existing):
    store = ProjectSessionStore(tmp_path / "sessions")
    if existing:
        with store.acquire(**{**scope, "run_key": "previous"}) as previous:
            finish(previous)
    with store.acquire(**scope) as lease:
        lease.start()
        lease.abort_unstarted()
        assert lease.started is False
        assert lease.session_id == (THREAD if existing else None)
        assert lease.cached_receipt is None
        assert not lease.receipt_path.exists()
        with pytest.raises(ProjectSessionError):
            lease.start()
        with pytest.raises(ProjectSessionError):
            lease.complete(ok=True, returncode=0)
    with store.acquire(**scope) as retry:
        assert retry.session_id == (THREAD if existing else None)
        finish(retry)


@pytest.mark.parametrize("observed", [
    '{"type":"turn.started"}', '{"type":"future.event"}', events(completed=False),
    '{"type":"error","message":"started but failed"}',
])
def test_abort_is_forbidden_once_any_execution_event_is_observed(tmp_path, scope, observed):
    store = ProjectSessionStore(tmp_path / "sessions")
    with store.acquire(**scope) as lease:
        lease.start()
        lease.observe_jsonl(observed)
        with pytest.raises(ProjectSessionRecoveryRequired, match="执行事件"):
            lease.abort_unstarted()
        assert lease.started is True
    with pytest.raises(ProjectSessionRecoveryRequired):
        with store.acquire(**scope):
            pytest.fail("an observed invocation remains uncertain")


def test_abort_requires_started_lease_and_cannot_erase_completed_receipt(tmp_path, scope):
    store = ProjectSessionStore(tmp_path / "sessions")
    with store.acquire(**scope) as lease:
        with pytest.raises(ProjectSessionError):
            lease.abort_unstarted()
        receipt = finish(lease)
        with pytest.raises(ProjectSessionError):
            lease.abort_unstarted()
        assert lease.cached_receipt == receipt
        assert lease.receipt_path.exists()
    with pytest.raises(ProjectSessionError, match="锁已释放"):
        lease.abort_unstarted()


@pytest.mark.parametrize("field", ["device_id", "project_slug", "conversation_id", "assigned_agent_id", "run_key"])
def test_no_missing_identity_falls_back_to_default_chat(tmp_path, scope, field):
    with pytest.raises(ProjectSessionError):
        with ProjectSessionStore(tmp_path / "sessions").acquire(**{**scope, field: None}):
            pytest.fail("identity is required")
