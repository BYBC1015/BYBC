from dataclasses import replace
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

from test_project_exec import SessionProcess, configuration, context, execute


def chat(tmp_path, key='first', **changes):
    ctx = context(tmp_path, key)
    ctx.update(execution_route='chat', assigned_agent_kind='controller', controller_read_only=False)
    ctx.update(changes)
    return ctx


def test_controller_followup_resumes_exact_conversation_and_keeps_model_and_receipt(tmp_path):
    config = configuration(tmp_path); process = SessionProcess()
    assert execute(config, process, chat(tmp_path)).ok
    second = chat(tmp_path, 'second', task_id=2)
    assert execute(config, process, second).ok
    command = process.calls[-1][0]
    assert 'resume' in command and command[-2] == process.session
    assert '--ignore-user-config' not in command
    assert execute(config, process, second).ok and len(process.calls) == 2
    assert len(list((tmp_path / 'project-sessions' / 'bindings').glob('*.json'))) == 1


def test_read_only_followup_does_not_wait_for_or_change_main_session(tmp_path):
    from codex_bridge.project_sessions import ProjectSessionStore
    config = configuration(tmp_path); process = SessionProcess()
    scope = dict(device_id=8, project_slug='project-one', conversation_id=12,
                 assigned_agent_id=2, project_path=config.projects['project-one'],
                 run_key='busy', generation=1)
    with ProjectSessionStore(tmp_path / 'project-sessions').acquire(**scope):
        assert execute(config, process, chat(tmp_path, 'question', controller_read_only=True)).ok
        assert 'resume' not in process.calls[-1][0]
        blocked = execute(config, process, chat(tmp_path, 'arrangement'))
        assert not blocked.ok and '总控对话' in blocked.error
    binding = json.loads(next((tmp_path / 'project-sessions' / 'bindings').glob('*.json')).read_text())
    assert binding['session_id'] is None


def test_other_conversations_never_resume_previous_room(tmp_path):
    config = configuration(tmp_path); process = SessionProcess()
    assert execute(config, process, chat(tmp_path)).ok
    import uuid
    process.session = str(uuid.uuid4())
    assert execute(config, process, chat(tmp_path, 'other-room', conversation_id=13)).ok
    assert 'resume' not in process.calls[-1][0]
    assert len(list((tmp_path / 'project-sessions' / 'bindings').glob('*.json'))) == 2


def test_live_session_identity_is_saved_before_cli_finishes(tmp_path):
    from codex_bridge.project_sessions import ProjectSessionStore
    from codex_bridge.codex_exec import CodexExecRunner
    import uuid
    project = tmp_path / 'project'; project.mkdir()
    marker = tmp_path / 'saved'
    session_id = str(uuid.uuid4())
    store = ProjectSessionStore(tmp_path / 'sessions')
    scope = dict(device_id=8, project_slug='project-one', conversation_id=12,
                 assigned_agent_id=2, project_path=project, run_key='live-identity', generation=1)
    script = ('import json,time,pathlib\n'
              f'print(json.dumps({{"type":"thread.started","thread_id":{session_id!r}}}),flush=True)\n'
              f'marker=pathlib.Path({str(marker)!r})\n'
              'end=time.monotonic()+4\n'
              'while not marker.exists() and time.monotonic()<end: time.sleep(.01)\n'
              'assert marker.exists(), "thread identity was not persisted while executing"\n'
              'print(json.dumps({"type":"turn.completed"}),flush=True)\n')
    with store.acquire(**scope) as lease:
        lease.start()
        def observe(line):
            lease.observe_jsonl(line)
            binding=json.loads(next((store.root/'bindings').glob('*.json')).read_text())
            if binding['session_id']==session_id:marker.touch()
        result=CodexExecRunner(SimpleNamespace())._run_cancellable(
            [sys.executable,'-c',script],'',8,{},threading.Event(),event_callback=observe)
        assert result.returncode==0
        assert lease.complete(ok=True,returncode=0)['session_id']==session_id
