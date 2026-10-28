import pytest
import os
import threading
from types import SimpleNamespace

from codex_bridge.agent_worker import filter_agent_inbox, mutex_name, main
from codex_bridge.client import HubClientError
from codex_bridge.config import BridgeConfig


def test_worker_filters_legacy_unassigned_foreign_and_completed_work():
    inbox = {"device": {"id": 7}, "mentions": [{"id": 99}], "tasks": [
        {"id": 1, "assigned_agent_id": 2, "assigned_device_id": 7, "status": "open"},
        {"id": 2, "assigned_agent_id": None, "assigned_device_id": 7, "status": "open"},
        {"id": 3, "assigned_agent_id": 2, "assigned_device_id": None, "status": "open"},
        {"id": 4, "assigned_agent_id": 2, "assigned_device_id": 8, "status": "open"},
        {"id": 5, "assigned_agent_id": 2, "assigned_device_id": 7, "status": "in_progress"},
        {"id": 6, "assigned_agent_id": 2, "assigned_device_id": 7, "status": "review"},
        {"id": 7, "assigned_agent_id": 2, "assigned_device_id": 7, "status": "claimed"},
    ]}
    filtered = filter_agent_inbox(inbox)
    assert [item["id"] for item in filtered["tasks"]] == [1, 7]
    assert filtered["mentions"] == []
    assert inbox["mentions"] == [{"id": 99}]
    recovered = filter_agent_inbox(inbox, {5, 6, 4})
    assert [item["id"] for item in recovered["tasks"]] == [1, 5, 6, 7]


def test_worker_rejects_unknown_connection_identity():
    with pytest.raises(HubClientError):
        filter_agent_inbox({"device": {"label": "Unknown"}, "tasks": []})


def test_worker_mutex_does_not_share_old_bridge_or_other_config(tmp_path):
    first = mutex_name(tmp_path / "one.json", "default")
    assert first == mutex_name(tmp_path / "one.json", "default")
    assert first != mutex_name(tmp_path / "two.json", "default")
    assert first != mutex_name(tmp_path / "one.json", "another")
    assert "CodexBridge.MultiAI" not in first


def test_once_reports_without_claiming_executing_or_writing_seen(tmp_path, monkeypatch):
    config_path = tmp_path / "worker.json"
    config_path.write_text("{}", encoding="utf-8")
    config = BridgeConfig(hub="https://hub.example", token="test", prompt_dir=tmp_path / "prompts",
                         state_path=tmp_path / "state.json", auto_execute_codex=True,
                         ignore_existing_on_first_run=False)
    monkeypatch.setattr("codex_bridge.agent_worker.load_config", lambda _: config)
    monkeypatch.setenv("CODEX_BRIDGE_AGENT_CONFIG", "")
    monkeypatch.setenv("PYTHONPATH", "")

    class Runner:
        def __init__(self, config, client):
            self.state = SimpleNamespace(task_deliveries={})

        def poll_status(self):
            return SimpleNamespace(device_label="Agent worker", task_count=0)

        def process_once(self):
            raise AssertionError("--once must not claim or run work")

    monkeypatch.setattr("codex_bridge.agent_worker.BridgeRunner", Runner)
    assert main(["--config", str(config_path), "--once"]) == 0
    assert os.environ["CODEX_BRIDGE_AGENT_CONFIG"] == str(config_path)
    assert not config.state_path.exists()


def prepare_worker_test(tmp_path, monkeypatch):
    path=tmp_path/'worker.json'
    path.write_text('{}')
    config=BridgeConfig(hub='https://hub.example', token='test', prompt_dir=tmp_path/'prompts',
        state_path=tmp_path/'state.json', auto_execute_codex=True, ignore_existing_on_first_run=False)
    monkeypatch.setattr('codex_bridge.agent_worker.load_config',lambda _:config)
    monkeypatch.setenv('CODEX_BRIDGE_AGENT_CONFIG','')
    monkeypatch.setenv('PYTHONPATH','')
    events=[]
    class Heartbeat:
        def __init__(self,*a,**k):self.thread=SimpleNamespace(is_alive=lambda:True)
        def start(self):events.append('heartbeat-start')
        def close(self):events.append('heartbeat-close')
    monkeypatch.setattr('codex_bridge.agent_worker.AgentWorkerHeartbeat',Heartbeat)
    class Runner:
        def __init__(self,config,client):self.state=SimpleNamespace(task_deliveries={})
        def process_once(self):return SimpleNamespace(messages=[])
    monkeypatch.setattr('codex_bridge.agent_worker.BridgeRunner',Runner)
    return path,events


def test_worker_initialization_failure_never_leaves_heartbeat_running(tmp_path,monkeypatch):
    path,events=prepare_worker_test(tmp_path,monkeypatch)
    def fail(*a,**k):raise RuntimeError('pool initialization failed')
    monkeypatch.setattr('codex_bridge.task_pool.DurableTaskPool',fail)
    stop=threading.Event()
    with pytest.raises(RuntimeError):main(['--config',str(path)],stop_event=stop,log_callback=lambda _:None)
    assert events==[]
    assert not stop.is_set()


def test_worker_cleanup_does_not_convert_unexpected_exit_to_user_stop(tmp_path,monkeypatch):
    path,events=prepare_worker_test(tmp_path,monkeypatch)
    class Pool:
        capacity=2
        def __init__(self,*a,**k):pass
        def tick(self):raise KeyboardInterrupt()
        def close(self):events.append('pool-close')
    monkeypatch.setattr('codex_bridge.task_pool.DurableTaskPool',Pool)
    stop=threading.Event()
    assert main(['--config',str(path)],stop_event=stop,log_callback=lambda _:None)==0
    assert events==['heartbeat-start','pool-close','heartbeat-close']
    assert not stop.is_set()


def test_shutdown_keeps_heartbeat_until_running_pool_work_drains(tmp_path,monkeypatch):
    path,events=prepare_worker_test(tmp_path,monkeypatch)
    closing,release,ticked=(threading.Event() for _ in range(3))
    class Pool:
        capacity=2
        def __init__(self,*a,**k):pass
        def tick(self):ticked.set();return []
        def close(self):closing.set();release.wait(2);events.append('pool-close')
    monkeypatch.setattr('codex_bridge.task_pool.DurableTaskPool',Pool)
    stop=threading.Event()
    thread=threading.Thread(target=lambda:main(['--config',str(path)],stop_event=stop,log_callback=lambda _:None))
    thread.start()
    try:
        assert ticked.wait(1)
        stop.set()
        assert closing.wait(1)
        assert thread.is_alive()
        assert 'heartbeat-close' not in events
    finally:
        release.set();stop.set();thread.join(3)
    assert not thread.is_alive()
    assert events[-2:]==['pool-close','heartbeat-close']


def test_stop_during_startup_never_launches_execution_lanes(tmp_path,monkeypatch):
    path,events=prepare_worker_test(tmp_path,monkeypatch)
    stop=threading.Event()
    class Pool:
        capacity=2
        def __init__(self,*a,**k):stop.set()
        def tick(self):raise AssertionError('must not claim work after stop')
        def close(self):events.append('pool-close')
    monkeypatch.setattr('codex_bridge.task_pool.DurableTaskPool',Pool)
    assert main(['--config',str(path)],stop_event=stop,log_callback=lambda _:None)==0
    assert events==['pool-close','heartbeat-close']
