"""Response latency regressions: real local pipes, no GUI or paid models."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from codex_bridge.agent_worker import main
from codex_bridge.attachments import prepare_task_attachments
from codex_bridge.attachment_cache import AttachmentCache
from codex_bridge.codex_stream import ProgressDispatcher, VisibleCodexProgress
from codex_bridge.task_pool import task_config
from test_agent_attachments import AttachmentClient, item, png
from test_agent_runtime import config_for
from test_agent_worker import prepare_worker_test


def test_worker_network_wait_does_not_hold_controller_polling_or_completion_wakeup(tmp_path, monkeypatch):
    path, events = prepare_worker_test(tmp_path, monkeypatch)
    entered, release, answered = (threading.Event() for _ in range(3))
    stop = threading.Event()
    pools = {}
    class Pool:
        capacity = 2
        def __init__(self, *args, lane='worker', **kwargs):
            self.lane = lane
            self.wakeup = threading.Event()
            pools[lane] = self
        def tick(self):
            if self.lane == 'worker':
                entered.set()
                release.wait(4)
            elif entered.is_set():
                answered.set()
            return []
        def close(self):events.append('pool-close')
    monkeypatch.setattr('codex_bridge.task_pool.DurableTaskPool', Pool)
    thread = threading.Thread(target=lambda: main(['--config', str(path)], stop_event=stop, log_callback=lambda _: None))
    thread.start()
    try:
        assert entered.wait(1)
        pools['controller'].wakeup.set()
        assert answered.wait(1), 'Chat scheduler waited behind a worker inbox call'
        assert not release.is_set()
    finally:
        stop.set()
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert events[-1] == 'heartbeat-close'


def cached_config(tmp_path):
    base = config_for(tmp_path)
    base.project_root = tmp_path / 'controller-workspaces'
    task = {'id': 42, 'project_slug': 'agent-test', 'assigned_agent_kind': 'worker'}
    return base, task_config(base, task)


def test_inherited_inputs_download_once_and_remain_independent_task_files(tmp_path):
    base, config = cached_config(tmp_path)
    data = png()
    client = AttachmentClient([(item(data, name='reference.png'), data)])
    first = prepare_task_attachments(client, config, 'agent-test', 42, config.projects['agent-test'])
    client.files[0][0]['task_id'] = 43
    next_config = task_config(base, {'id': 43, 'project_slug': 'agent-test', 'assigned_agent_kind': 'controller', 'conversation_id': 7})
    next_config.projects['agent-test'].mkdir(parents=True)
    second = prepare_task_attachments(client, next_config, 'agent-test', 43, next_config.projects['agent-test'])
    assert len(client.downloads) == 1
    assert client.manifest_calls == 2  # Permission and task membership are still checked.
    first_path, second_path = (Path(bundle.files[0]['local_path']) for bundle in (first, second))
    assert first_path != second_path and first_path.read_bytes() == second_path.read_bytes() == data
    second_path.write_bytes(b'local task edit')
    assert first_path.read_bytes() == data
    restored = prepare_task_attachments(client, next_config, 'agent-test', 43, next_config.projects['agent-test'])
    assert Path(restored.files[0]['local_path']).read_bytes() == data
    assert len(client.downloads) == 1


def test_bad_cache_is_a_miss_and_changed_manifest_never_reuses_old_bytes(tmp_path):
    _, config = cached_config(tmp_path)
    data = b'original brief'
    client = AttachmentClient([(item(data), data)])
    prepare_task_attachments(client, config, 'agent-test', 42, config.projects['agent-test'])
    cache_files = list(Path(config.backend_options['attachment_cache_root']).glob('*/*/*.blob'))
    cache_files[0].write_bytes(b'x' * len(data))
    client.files[0][0]['task_id'] = 43
    result = prepare_task_attachments(client, config, 'agent-test', 43, config.projects['agent-test'])
    assert Path(result.files[0]['local_path']).read_bytes() == data
    assert len(client.downloads) == 2
    new_data = b'new corrected brief'
    client.files = [(item(new_data, task_id=44), new_data)]
    changed = prepare_task_attachments(client, config, 'agent-test', 44, config.projects['agent-test'])
    assert Path(changed.files[0]['local_path']).read_bytes() == new_data
    assert len(client.downloads) == 3
    client.files = []
    assert prepare_task_attachments(client, config, 'agent-test', 45, config.projects['agent-test']).files == []


def test_identical_visual_bytes_only_enter_current_turn_once_but_keep_source_records(tmp_path):
    _, config = cached_config(tmp_path)
    data = png()
    client = AttachmentClient([(item(data, name='mother.png'), data),
        (item(data, attachment_id=2, name='resent-mother.png'), data)])
    result = prepare_task_attachments(client, config, 'agent-test', 42, config.projects['agent-test'])
    assert len(result.images) == 1 and len(result.files) == 2
    assert result.files[0]['image_path'] == result.files[1]['image_path']
    assert result.files[0]['local_path'] != result.files[1]['local_path']
    assert '字节完全一致' in result.files[1]['note']
    assert len(client.downloads) == 1


def test_cache_never_crosses_hub_or_project_and_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr('codex_bridge.attachment_cache.MAX_CACHE_FILES', 2)
    monkeypatch.setattr('codex_bridge.attachment_cache.MAX_CACHE_BYTES', 10)
    root = tmp_path / 'cache'
    cache = AttachmentCache(root, 'https://hub-one', 'agent-test')
    source = tmp_path / 'source'
    for data in (b'aaaa', b'bbbb', b'cccc'):
        source.write_bytes(data)
        cache.store(source, hashlib.sha256(data).hexdigest(), len(data))
    assert len(list(root.glob('*/*/*.blob'))) == 2
    assert sum(path.stat().st_size for path in root.glob('*/*/*.blob')) == 8
    sha = hashlib.sha256(b'cccc').hexdigest()
    assert not AttachmentCache(root, 'https://hub-two', 'agent-test').restore(tmp_path / 'cross-hub', sha, 4)
    assert not AttachmentCache(root, 'https://hub-one', 'other-project').restore(tmp_path / 'cross-project', sha, 4)
    assert source.read_bytes() == b'cccc'


def test_failed_cache_io_does_not_prevent_verified_download(tmp_path, monkeypatch):
    _, config = cached_config(tmp_path)
    data = b'authorized current input'
    client = AttachmentClient([(item(data), data)])
    def fail(*args):raise PermissionError('cache temporarily locked')
    monkeypatch.setattr(AttachmentCache, 'restore', fail)
    monkeypatch.setattr(AttachmentCache, 'store', fail)
    result = prepare_task_attachments(client, config, 'agent-test', 42, config.projects['agent-test'])
    assert Path(result.files[0]['local_path']).read_bytes() == data
    assert len(client.downloads) == 1


def test_public_answer_retries_transient_send_failure_without_another_model_event():
    delivered = threading.Event()
    calls = []
    def callback(kind, text):
        calls.append((kind, text))
        if len(calls) == 1:raise OSError('one failed POST')
        delivered.set()
    dispatcher = ProgressDispatcher(callback, interval=0)
    try:
        dispatcher.publish('assistant', '四个交付项已经确认，下一步按母版关系分派。')
        assert delivered.wait(1)
    finally:
        dispatcher.close()
    assert len(calls) == 2 and calls[0] == calls[1]


def test_permanent_send_failure_is_bounded_and_does_not_busy_retry():
    calls = []
    def callback(*args):calls.append(args);raise OSError('offline')
    dispatcher = ProgressDispatcher(callback, interval=0)
    try:
        dispatcher.publish('assistant', '真实正文')
        deadline = time.monotonic() + 2
        while len(calls) < 3 and time.monotonic() < deadline:time.sleep(.01)
        time.sleep(.15)
        assert len(calls) == 3
    finally:dispatcher.close()


def test_new_public_answer_is_sent_before_queued_generic_status():
    entered, release, answered = (threading.Event() for _ in range(3))
    calls = []
    def callback(kind, text):
        calls.append((kind, text))
        if len(calls) == 1:entered.set();release.wait(2)
        if kind == 'assistant':answered.set()
    dispatcher = ProgressDispatcher(callback, interval=0)
    try:
        dispatcher.publish('status', '开始')
        assert entered.wait(1)
        dispatcher.publish('status', '正在执行工具')
        dispatcher.publish('assistant', '已定位到具体问题。')
        release.set()
        assert answered.wait(1)
    finally:release.set();dispatcher.close()
    assert calls[1] == ('assistant', '已定位到具体问题。')


def test_completed_tools_clear_stale_stage_without_exposing_arguments_or_results():
    updates = []
    progress = VisibleCodexProgress(lambda *args: updates.append(args))
    def tool(event, identity, kind):
        progress.feed(json.dumps({'type': event, 'item': {'id': identity, 'type': kind,
            'arguments': {'token': 'private'}, 'result': 'private output'}}))
    tool('item.started', 'a', 'mcp_tool_call')
    tool('item.started', 'b', 'command_execution')
    tool('item.completed', 'a', 'mcp_tool_call')
    assert updates[-1] == ('status', '正在执行工具')
    tool('item.completed', 'b', 'command_execution')
    assert updates[-1] == ('status', '工具调用已结束，正在继续处理')
    assert 'private' not in str(updates)


@pytest.mark.parametrize('probe', ['process', 'desktop', 'path', 'git'])
def test_actual_probe_child_invalid_utf8_never_crashes_reader_thread(tmp_path, monkeypatch, probe):
    from codex_bridge.codex_app_probe import WindowsCodexAppProbe
    from codex_bridge.network_guard import WindowsProcessProbe
    from codex_bridge.tool_locator import find_running_codex_path
    from codex_bridge.runner import BridgeRunner
    original_run = subprocess.run
    expected_path = r'C:\Program Files\WindowsApps\OpenAI.Codex_example\app\Codex.exe'
    output = {'process': 'Codex.exe', 'desktop': 'CODEX_DESKTOP_RUNNING',
              'path': expected_path, 'git': 'a' * 40}[probe]
    seen = []
    def actual_bad_encoding_child(command, **kwargs):
        seen.append(kwargs)
        # Invalid startup diagnostics on stderr reproduce Windows communicate's
        # background reader failure; stdout still contains a valid probe result.
        script = f'import sys;sys.stderr.buffer.write(b"\\xff\\xfe\\n");sys.stdout.buffer.write({output.encode()!r})'
        return original_run([sys.executable, '-c', script], **kwargs)
    monkeypatch.setattr(subprocess, 'run', actual_bad_encoding_child)
    monkeypatch.setattr(sys, 'platform', 'win32')
    if probe == 'process':assert WindowsProcessProbe().is_running('Codex')
    elif probe == 'desktop':assert WindowsCodexAppProbe().is_running()
    elif probe == 'path':assert find_running_codex_path() == Path(expected_path)
    else:assert BridgeRunner._git_commit(tmp_path) == 'a' * 40
    assert seen[0]['encoding'] == 'utf-8' and seen[0]['errors'] == 'replace'
