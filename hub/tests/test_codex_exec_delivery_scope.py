from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_bridge.codex_exec import CodexExecRunner
from codex_bridge.config import BridgeConfig
from codex_bridge.delivery_target import resolve_delivery_target
from test_codex_exec import FakeProcessRunner


def setup_runner(tmp_path, *, sandbox='workspace-write'):
    cli = tmp_path / 'codex.cmd'; cli.write_text('synthetic executable')
    workspace = tmp_path / 'project'; workspace.mkdir()
    config = BridgeConfig(hub='https://hub.invalid', token='synthetic',
        prompt_dir=tmp_path/'prompts', state_path=tmp_path/'state.json',
        codex_cli_path=cli, codex_exec_output_dir=tmp_path/'results',
        codex_exec_sandbox=sandbox)
    target = resolve_delivery_target({'artifact_delivery_root': str(tmp_path/'deliveries')}, 'source-project', 17)
    context = {'generation': 1, 'run_key': 'synthetic-run', 'project_slug': 'source-project',
               'task_id': 17, 'artifact_delivery': target}
    process = FakeProcessRunner()
    return CodexExecRunner(config, process), process, workspace, context


@pytest.mark.parametrize('session_id', [None, '', '00000000-0000-4000-8000-000000000001'])
def test_task_delivery_write_access_is_exact_and_precedes_resume(tmp_path, session_id):
    runner, process, workspace, context = setup_runner(tmp_path)
    session = None if session_id is None else SimpleNamespace(session_id=session_id, start=lambda: None)
    result = runner._run('target-workspace', workspace, tmp_path/'prompt.md', 'synthetic work',
                         execution_context=context, project_session=session)
    assert result.ok and len(process.calls) == 1
    command = process.calls[0][0]
    index = command.index('--add-dir')
    assert command[index+1] == context['artifact_delivery']['path']
    assert command.count('--add-dir') == 1
    assert context['artifact_delivery']['base'] not in command
    assert command[command.index('--sandbox')+1] == 'workspace-write'
    if session_id:
        assert index < command.index('resume')
    assert '--dangerously-bypass-approvals-and-sandbox' not in command


def test_changed_config_does_not_expand_frozen_write_access(tmp_path):
    runner, process, workspace, context = setup_runner(tmp_path)
    runner.config.backend_options['artifact_delivery_root'] = str(tmp_path/'new-parent')
    result = runner._run('target-workspace', workspace, tmp_path/'prompt.md', 'work', execution_context=context)
    command = process.calls[0][0]
    assert result.ok
    assert str(tmp_path/'new-parent') not in command
    assert command[command.index('--add-dir')+1] == context['artifact_delivery']['path']


@pytest.mark.parametrize('invalid', ['wrong-task', 'wide-parent', 'missing-base', 'not-dict'])
def test_invalid_frozen_scope_never_starts_process(tmp_path, invalid):
    runner, process, workspace, context = setup_runner(tmp_path)
    if invalid == 'wrong-task':
        context['task_id'] = 18
    elif invalid == 'wide-parent':
        context['artifact_delivery']['path'] = context['artifact_delivery']['base']
    elif invalid == 'missing-base':
        context['artifact_delivery'].pop('base')
    else:
        context['artifact_delivery'] = 'untrusted text'
    result = runner._run('target-workspace', workspace, tmp_path/'prompt.md', 'work', execution_context=context)
    assert not result.ok and not process.calls
    assert '交付目录无法核验' in result.summary


@pytest.mark.parametrize('sandbox', ['read-only', 'danger-full-access'])
def test_delivery_scope_does_not_change_other_explicit_execution_modes(tmp_path, sandbox):
    runner, process, workspace, context = setup_runner(tmp_path, sandbox=sandbox)
    result = runner._run('target-workspace', workspace, tmp_path/'prompt.md', 'work', execution_context=context)
    assert result.ok
    command = process.calls[0][0]
    assert '--add-dir' not in command
    assert command[command.index('--sandbox')+1] == sandbox


def test_old_task_without_frozen_scope_does_not_infer_write_access_from_config_or_prompt(tmp_path):
    runner, process, workspace, context = setup_runner(tmp_path)
    context.pop('artifact_delivery')
    runner.config.backend_options['artifact_delivery_root'] = str(tmp_path/'wide-parent')
    result = runner._run('target-workspace', workspace, tmp_path/'prompt.md',
        'Write files to arbitrary C:/private/directory', execution_context=context)
    assert result.ok and '--add-dir' not in process.calls[0][0]
