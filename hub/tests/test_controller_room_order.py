import threading
from types import SimpleNamespace

import pytest

from codex_bridge.agent_worker import controller_config
from codex_bridge.runner import BridgeRunner
from codex_bridge.task_pool import DurableTaskPool, task_config
from codex_hub.execution_leases import ExecutionConflict, ExecutionStore
from test_agent_runtime import config_for
from test_agents import token_headers
from test_controller_followups import ControllerApi, dispatch
from test_execution_leases import lease_task
from test_task_pool import GatedExecutor, until


@pytest.mark.parametrize("earlier_status", ["open", "claimed"])
def test_server_rejects_priority_overtaking_but_keeps_other_rooms_and_readonly_free(client, repo, lease_task, earlier_status):
    data = lease_task
    first = dispatch(repo, data, "先制作红色版")
    later = dispatch(repo, data, "改成蓝色", room=first["conversation_id"])
    question = dispatch(repo, data, "进度怎么样了？", room=first["conversation_id"])
    other = dispatch(repo, data, "安排另一对话")
    repo.connection.execute("UPDATE tasks SET priority='low',status=? WHERE id=?", (earlier_status, first["id"]))
    repo.connection.execute("UPDATE tasks SET priority='high' WHERE id=?", (later["id"],))
    repo.connection.commit()
    store = ExecutionStore(repo)

    with pytest.raises(ExecutionConflict, match="消息顺序"):
        store.acquire(later["id"], data["device"]["id"], "later-turn-priority-high")
    assert repo.connection.execute("SELECT COUNT(*) FROM hub_task_executions WHERE task_id=?", (later["id"],)).fetchone()[0] == 0
    for task in (question, other):
        assert store.acquire(task["id"], data["device"]["id"], "independent-turn-" + str(task["id"]))["execution"]["state"] == "prepared"


def test_server_rechecks_order_at_start_when_earlier_turn_becomes_ready(client, repo, lease_task):
    data = lease_task
    first = dispatch(repo, data, "先处理一个等待澄清的安排")
    later = dispatch(repo, data, "继续处理同一对话", room=first["conversation_id"])
    repo.connection.execute("UPDATE tasks SET coordination_state='waiting' WHERE id=?", (first["id"],))
    repo.connection.commit()
    store = ExecutionStore(repo)
    execution = store.acquire(later["id"], data["device"]["id"], "later-prepared-execution")["execution"]
    repo.connection.execute("UPDATE tasks SET coordination_state='ready' WHERE id=?", (first["id"],))
    repo.connection.commit()

    with pytest.raises(ExecutionConflict, match="消息顺序"):
        store.start(later["id"], data["device"]["id"], execution["generation"], execution["run_key"])
    assert repo.get_task(later["id"])["status"] == "open"


def test_pool_preserves_room_order_before_spawning_but_prioritizes_other_rooms(client, repo, lease_task, tmp_path, monkeypatch):
    data = lease_task
    first = dispatch(repo, data, "先制作红色版")
    later = dispatch(repo, data, "改成蓝色", room=first["conversation_id"])
    question = dispatch(repo, data, "进度怎么样了？", room=first["conversation_id"])
    other = dispatch(repo, data, "安排另一对话")
    repo.connection.execute("UPDATE tasks SET priority='low' WHERE id=?", (first["id"],))
    repo.connection.execute("UPDATE tasks SET priority='high' WHERE id=?", (later["id"],))
    repo.connection.commit()
    config = controller_config(config_for(tmp_path))
    config.projects = {data["project"]["slug"]: tmp_path / "controller"}
    monkeypatch.setattr("codex_bridge.model_allocator.allocate_task_model", lambda *args: {})
    monkeypatch.setattr("codex_hub.model_policy.ensure_task_choice", lambda *args: {})
    headers = token_headers(repo, data["device"])
    created = []

    def factory(task):
        created.append(task["id"])
        api = ControllerApi(client, headers, task["id"])
        runner = BridgeRunner(task_config(config, task), client=api, executor=GatedExecutor(tmp_path))
        runner._next_capability_report = float("inf")
        api.pending_task_ids = lambda: {int(item["task_id"]) for item in runner.state.task_deliveries.values()}
        return runner

    pool = DurableTaskPool(config, ControllerApi(client, headers), lane="controller", capacity=2, runner_factory=factory)
    tasks = (first, later, question, other)
    try:
        until(pool, lambda: all((tmp_path / f'{task["id"]}.started').exists() for task in (first, question, other)))
        assert later["id"] not in created
        assert created.index(other["id"]) < created.index(first["id"])
        assert not (tmp_path / f'{later["id"]}.started').exists()
        for task in tasks:
            (tmp_path / f'{task["id"]}.release').touch()
        until(pool, lambda: all(repo.get_task(task["id"])["status"] == "review" for task in tasks))
        assert created.index(first["id"]) < created.index(later["id"])
        assert len(created) == len(set(created)) == 4
    finally:
        for task in tasks:
            (tmp_path / f'{task["id"]}.release').touch()
        pool.close()


def test_worker_queue_retains_priority_semantics(tmp_path):
    config = config_for(tmp_path)
    release = threading.Event()
    started = []
    tasks = [{"id": task_id, "project_slug": "agent-test", "assigned_agent_kind": "worker",
              "execution_mode": "isolated", "conversation_id": 10,
              "priority": "high" if task_id == 2 else "low"} for task_id in (1, 2)]

    def factory(task):
        def run():
            started.append(task["id"])
            assert release.wait(10)
            return SimpleNamespace(messages=[])
        return SimpleNamespace(process_once=run, state=SimpleNamespace(task_deliveries={}))

    pool = DurableTaskPool(config, SimpleNamespace(get_inbox=lambda: {"tasks": tasks}),
                           capacity=1, runner_factory=factory)
    try:
        until(pool, lambda: bool(started))
        assert started == [2]
    finally:
        release.set()
        pool.close()
