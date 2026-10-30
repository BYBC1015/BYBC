"""Project routing exercises the real CLI command builder and durable receipts."""
import json
import subprocess
import uuid
import threading
from dataclasses import replace
from pathlib import Path

from codex_bridge.codex_exec import CodexExecRunner
from codex_bridge.config import BridgeConfig
from codex_bridge.task_pool import task_config


class SessionProcess:
    def __init__(self):
        self.calls = []
        self.session = str(uuid.uuid4())
        self.fail = False

    def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        output = Path(command[command.index('--output-last-message') + 1])
        output.write_text('已处理本轮工程', encoding='utf-8')
        stdout = json.dumps({'type': 'thread.started', 'thread_id': self.session}) + '\n'
        stdout += json.dumps({'type': 'turn.failed' if self.fail else 'turn.completed'})
        return subprocess.CompletedProcess(command, 1 if self.fail else 0, stdout, '')


def configuration(tmp_path):
    project = tmp_path / 'project'; project.mkdir()
    cli = tmp_path / 'codex.exe'; cli.touch()
    return BridgeConfig(hub='https://hub.example', token='test', state_path=tmp_path / 'state.json',
        prompt_dir=tmp_path / 'prompts', projects={'project-one': project}, codex_cli_path=cli,
        codex_exec_output_dir=tmp_path / 'results', codex_exec_ignore_user_config=False)


def context(tmp_path, key='run-one', **fields):
    return dict(execution_route='project', device_id=8, assigned_agent_id=2, conversation_id=12,
        task_id=1, project_slug='project-one', generation=1, run_key=key,
        receipt_path=str(tmp_path / (key + '.completion.json')), **fields)


def execute(config, process, ctx):
    return CodexExecRunner(config, process).run('project-one', config.projects['project-one'],
        config.prompt_dir / (ctx['run_key'] + '.md'), '当前任务', execution_context=ctx)


def test_project_resume_keeps_directory_model_mcp_images_and_receipt(tmp_path):
    config = configuration(tmp_path); process = SessionProcess()
    first = execute(config, process, context(tmp_path))
    assert first.ok
    assert '--json' in process.calls[0][0] and 'resume' not in process.calls[0][0]
    image = config.projects['project-one'] / 'input.png'; image.touch()
    second_ctx = context(tmp_path, 'run-two')
    second = CodexExecRunner(config, process).run('project-one', config.projects['project-one'],
        config.prompt_dir / 'two.md', '继续', execution_context=second_ctx, image_paths=[image],
        execution_preferences={'model': 'gpt-6.1-sol', 'reasoning_effort': 'high'})
    command, options = process.calls[-1]
    assert second.ok and command[-2:] == [process.session, '-']
    assert command.index('--cd') < command.index('resume') < command.index('--json')
    assert command.index('--sandbox') < command.index('resume') < command.index('--image')
    assert '--ignore-user-config' not in command and '--last' not in command
    assert options['cwd'] == str(config.projects['project-one'])
    assert 'gpt-6.1-sol' in command and 'model_reasoning_effort="high"' in command
    assert json.loads(Path(second_ctx['receipt_path']).read_text(encoding='utf-8'))['ok']
    replay = execute(config, process, second_ctx)
    assert replay.ok and len(process.calls) == 2


def test_direct_chat_do_not_create_project_session_and_missing_binding_fails(tmp_path):
    config = configuration(tmp_path); process = SessionProcess()
    for route in ('chat', 'direct'):
        ctx = context(tmp_path, route); ctx['execution_route'] = route
        assert execute(config, process, ctx).ok
    assert not (tmp_path / 'project-sessions').exists()
    invalid = context(tmp_path); invalid['conversation_id'] = None
    assert not execute(config, process, invalid).ok and len(process.calls) == 2


def test_task_specific_paths_share_one_registry_and_changed_workspace_blocks(tmp_path):
    base = configuration(tmp_path); process = SessionProcess()
    one = task_config(base, {'id': 1, 'project_slug': 'project-one'})
    two = task_config(base, {'id': 2, 'project_slug': 'project-one'})
    assert one.backend_options['project_session_root'] == two.backend_options['project_session_root']
    assert execute(one, process, context(tmp_path)).ok
    assert execute(two, process, context(tmp_path, 'next')).ok
    assert 'resume' in process.calls[-1][0]
    other = tmp_path / 'other'; other.mkdir()
    changed = replace(two, projects={'project-one': other})
    assert not execute(changed, process, context(tmp_path, 'moved')).ok
    assert len(process.calls) == 2


def test_mismatched_resume_is_not_success_or_retried_as_new(tmp_path):
    config = configuration(tmp_path); process = SessionProcess()
    assert execute(config, process, context(tmp_path)).ok
    process.session = str(uuid.uuid4())
    assert not execute(config, process, context(tmp_path, 'wrong')).ok
    assert not execute(config, process, context(tmp_path, 'retry')).ok
    assert len(process.calls) == 2


def test_failed_turn_keeps_original_session_for_next_attempt(tmp_path):
    config = configuration(tmp_path); process = SessionProcess(); process.fail = True
    assert not execute(config, process, context(tmp_path)).ok
    process.fail = False
    assert execute(config, process, context(tmp_path, 'new-attempt')).ok
    assert 'resume' in process.calls[-1][0]


def test_cancel_before_launch_does_not_poison_next_project_turn(tmp_path):
    config=configuration(tmp_path);process=SessionProcess();cancelled=threading.Event();cancelled.set()
    result=CodexExecRunner(config,process).run('project-one',config.projects['project-one'],
        tmp_path/'cancel.md','work',execution_context=context(tmp_path),cancel_event=cancelled)
    assert not result.ok and not process.calls
    assert execute(config,process,context(tmp_path,'next')).ok


def test_process_creation_failure_is_proven_unstarted(tmp_path,monkeypatch):
    config=configuration(tmp_path)
    def fail(*args,**kwargs):raise OSError('simulated launch failure')
    monkeypatch.setattr('codex_bridge.codex_exec.subprocess.Popen',fail)
    result=CodexExecRunner(config).run('project-one',config.projects['project-one'],
        tmp_path/'failed.md','work',execution_context=context(tmp_path))
    assert not result.ok and 'could not start' in result.error
    process=SessionProcess()
    assert execute(config,process,context(tmp_path,'next')).ok
