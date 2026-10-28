"""Actual child processes exercise streaming without invoking a paid model."""
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_bridge.codex_exec import CodexExecRunner
from codex_bridge.codex_stream import ProgressDispatcher, VisibleCodexProgress, public_message
from test_project_exec import configuration, context


def item(text, item_id='a', event='item.updated', item_type='agent_message', **fields):
    return json.dumps({'type': event, 'item': {'id': item_id, 'type': item_type, 'text': text, **fields}})


def test_only_public_agent_text_and_real_generic_tool_states_are_exposed():
    updates = []
    parser = VisibleCodexProgress(lambda *values: updates.append(values))
    parser.feed('raw stderr prompt/secret')
    parser.feed(json.dumps({'type': 'thread.started', 'thread_id': 'private-session'}))
    parser.feed(item('private reasoning', item_type='reasoning'))
    parser.feed(item('private command output', event='item.started', item_type='command_execution', command='secret command'))
    parser.feed(item('private MCP arguments', event='item.started', item_type='mcp_tool_call', arguments={'token': 'secret'}))
    parser.feed(json.dumps({'type': 'error', 'message': 'prompt with credentials'}))
    parser.feed(json.dumps({'type': 'item/agentMessage/delta', 'delta': 'wrong protocol'}))
    assert updates == [('status', '正在执行工具'), ('status', '正在调用工具')]
    parser.feed(item('正在核对'))
    parser.feed(item('正在核对两个结果。'))
    parser.feed(item('正在核对两个结果。', event='item.completed'))
    parser.feed(item('已完成第一部分。', item_id='b', event='item.completed'))
    assert [text for kind, text in updates if kind == 'assistant'] == [
        '正在核对', '正在核对两个结果。', '正在核对两个结果。\n\n已完成第一部分。']


@pytest.mark.parametrize('marker', ['hub-reflection', 'hub-artifacts', 'think', 'analysis'])
def test_internal_blocks_and_every_partial_marker_are_withheld(marker):
    updates = []
    parser = VisibleCodexProgress(lambda *values: updates.append(values))
    opening = f'<{marker}>'
    for end in range(1, len(opening) + 1):
        parser.feed(item('公开回复\n' + opening[:end]))
        assert public_message('公开回复\n' + opening[:end]) == '公开回复'
    parser.feed(item(f'公开回复\n<{marker}>private receipt/paths'))
    parser.feed(item(f'公开回复\n<{marker}>private receipt/paths</{marker}>'))
    parser.feed(item(f'公开回复\n<{marker}>private receipt/paths</{marker}>\n后续说明'))
    assert [text for kind, text in updates if kind == 'assistant'] == ['公开回复', '公开回复\n\n后续说明']
    assert public_message('正文 <hub-artifacts format="json"') == '正文'


def test_snapshot_correction_cannot_rewrite_an_already_visible_prefix_and_length_is_stable():
    updates = []
    parser = VisibleCodexProgress(lambda *values: updates.append(values))
    parser.feed(item('已经看到的内容'))
    parser.feed(item('替换之前的内容'))
    parser.feed(item('已经看到的内容' + '长' * 25000))
    parser.feed(item('已经看到的内容' + '长' * 26000))
    assert len(updates) == 2 and len(updates[-1][1]) == 24000
    assert updates[-1][1].startswith(updates[0][1])


def test_dispatcher_coalesces_and_does_not_wait_for_a_blocked_callback():
    entered, release = threading.Event(), threading.Event()
    calls = []
    def callback(kind, text):
        calls.append((kind, text))
        entered.set()
        release.wait(3)
    dispatcher = ProgressDispatcher(callback, interval=0)
    try:
        dispatcher.publish('status', '开始')
        assert entered.wait(2)
        started = time.monotonic()
        for count in range(1000):
            dispatcher.publish('assistant', '文' * (count + 1))
        dispatcher.close(timeout=.01)
        assert time.monotonic() - started < 1
        assert len(calls) == 1
    finally:
        release.set()
        dispatcher.close()
    assert not dispatcher._thread.is_alive()


def test_failed_progress_delivery_does_not_stop_later_updates():
    called = threading.Event()
    updates = []
    def callback(kind, text):
        updates.append((kind, text))
        called.set()
        if len(updates) == 1:
            raise OSError('offline')
    dispatcher = ProgressDispatcher(callback, interval=0)
    dispatcher.publish('status', '开始')
    assert called.wait(2)
    dispatcher.publish('assistant', '继续完成')
    dispatcher.close()
    assert updates == [('status', '开始'), ('assistant', '继续完成')]


def test_actual_child_streams_before_exit_and_preserves_both_full_pipes(tmp_path):
    acknowledged = tmp_path / 'visible'
    before = item('正在处理，你可以继续留言。', event='item.completed')
    after = item('执行完成。', item_id='b', event='item.completed')
    script = ('import pathlib,sys,time\n'
              'prompt=sys.stdin.read()\n'
              f'print({before!r},flush=True)\n'
              f'marker=pathlib.Path({str(acknowledged)!r})\n'
              'deadline=time.monotonic()+4\n'
              'while not marker.exists() and time.monotonic()<deadline: time.sleep(.01)\n'
              'assert marker.exists(), "no progress before exit"\n'
              'assert prompt == "the prompt once"\n'
              'sys.stderr.write("s"*200000);sys.stderr.flush()\n'
              f'print({after!r},flush=True)\n')
    updates = []
    def callback(kind, text):
        updates.append((kind, text))
        if kind == 'assistant':
            acknowledged.touch()
    runner = CodexExecRunner(SimpleNamespace(token='configured-secret'))
    result = runner._run_cancellable([sys.executable, '-c', script], 'the prompt once', 6,
        {}, threading.Event(), progress_callback=callback)
    assert result.returncode == 0 and acknowledged.exists(), result.stderr
    assert result.stdout == before + '\n' + after + '\n'
    assert result.stderr == 's' * 200000
    assert updates[-1] == ('assistant', '正在处理，你可以继续留言。\n\n执行完成。')


def test_streaming_cancel_stops_owned_child_tree_and_drains_large_unterminated_line(tmp_path):
    marker = tmp_path / 'child.pid'
    child = f'import os,pathlib,time;pathlib.Path({str(marker)!r}).write_text(str(os.getpid()));time.sleep(30)'
    script = ('import subprocess,sys,time\n'
              f'subprocess.Popen([sys.executable,"-c",{child!r}])\n'
              f'print({item("收到，正在处理", event="item.completed")!r},flush=True)\n'
              'sys.stdout.write("x"*200000);sys.stdout.flush();time.sleep(30)\n')
    cancel = threading.Event()
    def trigger():
        until = time.monotonic() + 4
        while not marker.exists() and time.monotonic() < until:
            time.sleep(.01)
        cancel.set()
    trigger_thread = threading.Thread(target=trigger)
    trigger_thread.start()
    result = CodexExecRunner(SimpleNamespace())._run_cancellable([sys.executable, '-c', script], '', 8,
        {}, cancel, progress_callback=lambda *_: None)
    trigger_thread.join(2)
    assert result.returncode != 0 and 'Hub 已明确撤销' in result.stderr
    assert result.stdout.endswith('x' * 200000)
    assert marker.exists()
    import psutil
    try:
        child_process = psutil.Process(int(marker.read_text()))
        assert not child_process.is_running() or child_process.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        pass


def test_streaming_timeout_retains_output_and_kills_process(tmp_path):
    event = item('已启动本轮工作', event='item.completed')
    script = f'import time,sys;print({event!r},flush=True);print("diagnostic",file=sys.stderr,flush=True);time.sleep(20)'
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        CodexExecRunner(SimpleNamespace())._run_cancellable([sys.executable, '-c', script], '', .3,
            {}, threading.Event(), progress_callback=lambda *_: None)
    assert failure.value.stdout == event + '\n'
    assert failure.value.stderr == 'diagnostic\n'


def test_failed_start_releases_project_start_marker_without_progress(monkeypatch):
    def cannot_start(*args, **kwargs):
        raise OSError('executable unavailable')
    monkeypatch.setattr(subprocess, 'Popen', cannot_start)
    aborts, updates = [], []
    with pytest.raises(OSError):
        CodexExecRunner(SimpleNamespace())._run_cancellable(['unused'], '', 1, {}, threading.Event(),
            on_unstarted=lambda: aborts.append(1), progress_callback=lambda *values: updates.append(values))
    assert aborts == [1] and updates == []


def test_actual_child_still_completes_when_progress_callback_is_blocked():
    entered, release = threading.Event(), threading.Event()
    script = ('import sys\n'
              f'print({item("可以继续工作", event="item.completed")!r},flush=True)\n'
              'sys.stderr.write("e"*200000);sys.stderr.flush()\n')
    def slow_callback(*_):
        entered.set()
        release.wait(10)
    started = time.monotonic()
    try:
        result = CodexExecRunner(SimpleNamespace())._run_cancellable([sys.executable, '-c', script], '', 5,
            {}, threading.Event(), progress_callback=slow_callback)
        assert entered.is_set() and result.returncode == 0
        assert result.stderr == 'e' * 200000 and time.monotonic() - started < 3
    finally:
        release.set()


def test_real_project_stream_preserves_session_and_authoritative_completion(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    session_id = str(uuid.uuid4())
    original_popen = subprocess.Popen
    commands, updates = [], []
    def local_child(command, **kwargs):
        commands.append(command)
        output = command[command.index('--output-last-message') + 1]
        events = [json.dumps({'type': 'thread.started', 'thread_id': session_id}),
                  item('可见内容\n<hub-reflection>hidden</hub-reflection>', event='item.completed'),
                  json.dumps({'type': 'turn.completed'})]
        script = ('import pathlib,sys\n'
                  'sys.stdin.read()\n'
                  f'pathlib.Path({output!r}).write_text("最终权威结果",encoding="utf-8")\n'
                  f'print({chr(10).join(events)!r},flush=True)\n')
        return original_popen([sys.executable, '-c', script], **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', local_child)
    for key in ('first', 'second'):
        ctx = context(tmp_path, key)
        result = CodexExecRunner(config).run('project-one', config.projects['project-one'],
            config.prompt_dir / f'{key}.md', '工作', execution_context=ctx,
            progress_callback=lambda *values: updates.append(values))
        assert result.ok and result.last_message == '最终权威结果'
        receipt = json.loads(Path(ctx['receipt_path']).read_text(encoding='utf-8'))
        assert receipt['ok'] and receipt['last_message'] == '最终权威结果'
        assert 'thread.started' in result.stdout and 'turn.completed' in result.stdout
    assert commands[1][-2] == session_id and 'resume' in commands[1]
    assert commands[1].count('--json') == 1
    assert ('assistant', '可见内容') in updates
    assert all('hidden' not in text and 'hub-reflection' not in text for kind, text in updates)


def test_cancelled_before_stream_start_never_launches_or_publishes(monkeypatch):
    event = threading.Event(); event.set()
    def must_not_start(*args, **kwargs):
        raise AssertionError('unexpected launch')
    monkeypatch.setattr(subprocess, 'Popen', must_not_start)
    aborts, updates = [], []
    result = CodexExecRunner(SimpleNamespace())._run_cancellable(['unused'], '', 1, {}, event,
        on_unstarted=lambda: aborts.append(1), progress_callback=lambda *values: updates.append(values))
    assert result.returncode == -1 and aborts == [1] and updates == []


@pytest.mark.parametrize('route', ['chat', 'direct'])
def test_every_leased_route_uses_json_without_creating_project_sessions(tmp_path, route):
    from test_project_exec import SessionProcess
    config = configuration(tmp_path)
    process = SessionProcess()
    ctx = context(tmp_path, route); ctx['execution_route'] = route
    result = CodexExecRunner(config, process).run('project-one', config.projects['project-one'],
        config.prompt_dir / 'prompt.md', '任务', execution_context=ctx)
    assert result.ok and process.calls[0][0].count('--json') == 1
    assert not (tmp_path / 'project-sessions').exists()
