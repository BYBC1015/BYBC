import threading
import time
from types import SimpleNamespace

from codex_bridge.agent_service import AgentService


def wait_until(predicate, seconds=2):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate(): return
        time.sleep(.005)
    assert predicate()


def make_service(tmp_path, worker, **kwargs):
    config = tmp_path / 'config.json'
    config.write_text('{}')
    return AgentService(config, run_worker=worker, retry_seconds=.01, **kwargs)


def test_start_is_idempotent_and_stop_waits_for_running_work(tmp_path):
    entered, contacted, release = (threading.Event() for _ in range(3))
    calls = []

    def worker(argv, *, stop_event, status_callback, log_callback):
        calls.append(argv)
        entered.set()
        contacted.wait(2)
        status_callback({'connected': True, 'device': {'label': 'BYBC-test'}})
        stop_event.wait(2)
        release.wait(2)
        return 0

    service = make_service(tmp_path, worker)
    try:
        service.start()
        assert entered.wait(1)
        assert not service.snapshot()['online']
        service.start()
        assert len(calls) == 1
        contacted.set()
        wait_until(lambda: service.snapshot()['online'])
        assert service.snapshot()['device'] == 'BYBC-test'
        service.stop()
        assert service.is_running
        assert service.snapshot()['status'] == '正在收尾'
        service._status({'connected': True})
        assert not service.snapshot()['online']
    finally:
        contacted.set(); release.set(); service.stop()
        wait_until(lambda: not service.is_running)
    assert len(calls) == 1
    assert service.snapshot()['status'] == '已停止'


def test_crash_retries_without_disclosing_error_payload(tmp_path):
    calls, messages = [], []
    def worker(argv, *, stop_event, status_callback, log_callback):
        calls.append(argv)
        if len(calls) == 1: raise RuntimeError('private-token-not-for-log')
        status_callback({'connected': True})
        stop_event.wait(2)
        return 0
    service = make_service(tmp_path, worker, log_callback=messages.append)
    try:
        service.start()
        wait_until(lambda: service.snapshot()['online'])
        assert len(calls) == 2
        assert any('RuntimeError' in message for message in messages)
        assert not any('private-token' in message for message in messages)
    finally:
        service.stop(); wait_until(lambda: not service.is_running)


def test_external_worker_is_never_reported_as_owned_online(tmp_path):
    first = threading.Event()
    def worker(argv, *, stop_event, status_callback, log_callback):
        status_callback({'external': True})
        first.set()
        return 0
    service = make_service(tmp_path, worker)
    try:
        service.start(); assert first.wait(1)
        assert not service.snapshot()['online']
        assert '等待接管' in service.snapshot()['status']
    finally:
        service.stop(); wait_until(lambda: not service.is_running)


def test_presence_expires_and_real_contact_restores_it(tmp_path):
    def worker(argv, *, stop_event, status_callback, log_callback):
        status_callback({'connected': True})
        stop_event.wait(2)
    service = make_service(tmp_path, worker)
    try:
        service.start(); wait_until(lambda: service.snapshot()['online'])
        with service._lock: service._last_contact = time.monotonic() - 121
        assert not service.snapshot()['online']
        service._status({'connected': True})
        assert service.snapshot()['online']
    finally:
        service.stop(); wait_until(lambda: not service.is_running)


def test_missing_config_does_not_launch_or_pretend_online(tmp_path):
    service = AgentService(tmp_path / 'absent.json', run_worker=lambda *_a, **_k: 1/0)
    service.start()
    assert not service.is_running
    assert service.snapshot()['status'] == '未配置'


def test_gui_lists_durable_service_separately_from_legacy_bindings():
    from codex_bridge.gui import BridgeApp
    app = object.__new__(BridgeApp)
    app.config = SimpleNamespace(interval_seconds=20, bindings=[SimpleNamespace(
        id='codex', name='Codex', execution_backend='codex', enabled=True, interval_seconds=20)])
    agent = {'id':'hub-agents','name':'Hub 智能体','backend':'Hub / Codex CLI',
             'device':'BYBC-test','status':'连接中断 · 自动重试','interval':3}
    rows = app._binding_rows([agent, {'id':'codex','device':'legacy','status':'在线'}])
    assert rows[0] == agent
    assert rows[1]['status'] == '在线'
    assert len(rows) == 2


def test_exit_keeps_window_alive_until_agent_work_finishes(tmp_path, monkeypatch):
    from codex_bridge.gui import BridgeApp
    from codex_bridge import desktop_window
    calls = []
    app = object.__new__(BridgeApp)
    app.config_path = tmp_path / 'isolated-bridge-config.json'
    monkeypatch.setattr(desktop_window, 'request_desktop_close',
                        lambda path: calls.append(('desktop-close', path)))
    app.agent_service = SimpleNamespace(is_running=True)
    app.controller = SimpleNamespace(is_running=True)
    app._exit_stop_thread = SimpleNamespace(is_alive=lambda:False)
    app.root = SimpleNamespace(after=lambda *args:calls.append('wait'),destroy=lambda:calls.append('destroy'))
    app.tray = SimpleNamespace(stop=lambda:calls.append('tray-stop'))
    app.render_log = lambda:calls.append('render')
    app._finish_exit()
    assert 'destroy' not in calls
    assert not any(isinstance(call, tuple) for call in calls)
    app.agent_service.is_running=False
    app._finish_exit()
    assert 'destroy' not in calls
    app.controller.is_running=False
    app._finish_exit()
    assert calls[-3:] == ['tray-stop', ('desktop-close', app.config_path), 'destroy']
