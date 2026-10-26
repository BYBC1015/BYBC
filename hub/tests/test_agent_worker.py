import pytest
import os
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
