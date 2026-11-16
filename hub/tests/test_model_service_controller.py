"""The legacy Bridge GUI starts probes without blocking the work lanes."""
import threading
import time
from types import SimpleNamespace

from codex_bridge.client import HubClient
from codex_bridge.controller import BridgeController
from codex_bridge.runner import ProcessResult


def test_gui_controller_owns_independent_probe_lifecycle_and_once_does_not_start_it(monkeypatch):
    made = []
    worked = threading.Event()

    class Probe:
        def __init__(self, client):
            self.client = client
            self.stopped = threading.Event()
            self.thread = threading.Thread(target=lambda: self.stopped.wait(5), daemon=True)
            made.append(self)

        def start(self):
            self.thread.start()

    monkeypatch.setattr('codex_bridge.model_service_client.ModelServiceProbeWorker', Probe)
    client = HubClient('https://synthetic.invalid', 'synthetic-token')
    runner = SimpleNamespace(client=client,
        process_once=lambda: (worked.set() or ProcessResult()), poll_status=lambda: ProcessResult())
    config = SimpleNamespace(binding_id='one', execution_backend='codex', interval_seconds=3,
                             heartbeat_interval_seconds=3)
    guard = SimpleNamespace(ensure_ready=lambda **kwargs: None)
    controller = BridgeController(config, runner_factory=lambda _: runner, network_guard=guard)
    controller.run_once()
    assert not made
    worked.clear()
    controller.start()
    try:
        assert worked.wait(2) and len(made) == 1
        assert made[0].thread.is_alive() and made[0].client is not client
        assert made[0].client.token == client.token
    finally:
        controller.stop()
    assert made[0].stopped.is_set() and not made[0].thread.is_alive()
    controller.start()
    try:
        assert len(made) == 2
    finally:
        controller.stop()
