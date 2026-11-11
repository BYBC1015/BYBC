import json
import os
from pathlib import Path
import subprocess

import pytest

from codex_bridge.delivery_target import resolve_delivery_target
from test_agent_runtime import REFLECTION
from test_execution_recovery import runtime, restart


@pytest.fixture(autouse=True)
def isolated_model_allocator(monkeypatch):
    monkeypatch.setattr('codex_bridge.model_allocator.allocate_task_model', lambda *args: {})


def result_body(paths):
    links = '\n'.join(f'[{Path(p).name}]({p})' for p in paths)
    return (links + '\n<hub-artifacts>' + json.dumps([{'path': str(p)} for p in paths]) +
            '</hub-artifacts>\n<hub-reflection>' + json.dumps(REFLECTION) + '</hub-reflection>')


def test_good_report_survives_unsupported_xml(repo, tmp_path):
    runner, client, executor = runtime(repo, tmp_path)
    for name in ('report.md', 'tests.xml'):
        (tmp_path / 'project' / name).write_text('synthetic test evidence')
    executor.summary = result_body(['report.md', 'tests.xml'])
    uploads = []
    client.upload_task_artifact = lambda *args: uploads.append(args)
    runner.process_once()
    assert len(uploads) == len(executor.calls) == 1
    assert uploads[0][4] == 'report.md'
    assert repo.get_task(client.task_id)['status'] == 'blocked'
    body = repo.connection.execute("SELECT body_markdown FROM entries WHERE task_id=? AND kind='markdown'",
                                   (client.task_id,)).fetchone()[0]
    assert '[report.md](report.md)' in body
    assert 'tests.xml' in body and '不支持' in body
    assert '(tests.xml)' not in body and '<hub-artifacts>' not in body
    assert runner.state.get_task_delivery('agent-test', client.task_id) is None


def test_completion_retry_preserves_publication_even_if_files_are_deleted(repo, tmp_path):
    runner, client, executor = runtime(repo, tmp_path)
    report = tmp_path / 'project' / 'report.md'
    report.write_text('immutable delivered result')
    executor.summary = result_body(['report.md', 'missing.png'])
    uploads = []
    client.upload_task_artifact = lambda *args: uploads.append(args)
    client.fail_complete_before = True
    runner.process_once()
    receipt = runner.state.get_task_delivery('agent-test', client.task_id)
    frozen = receipt['artifact_publication']['body']
    assert receipt['result_status'] == 'blocked'
    report.unlink()
    client.fail_complete_before = False
    client.hide_inbox = True
    restored = restart(runner, client, executor)
    restored.process_once()
    assert len(executor.calls) == len(uploads) == 1
    assert repo.get_task(client.task_id)['status'] == 'blocked'
    body = repo.connection.execute("SELECT body_markdown FROM entries WHERE task_id=? AND kind='markdown'",
                                   (client.task_id,)).fetchone()[0]
    assert body == frozen
    assert restored.state.get_task_delivery('agent-test', client.task_id) is None


def test_task_delivery_directory_is_scoped_and_frozen(repo, tmp_path):
    runner, client, executor = runtime(repo, tmp_path)
    delivery_base = tmp_path / '下载成果 中文'
    runner.config.backend_options['artifact_delivery_root'] = str(delivery_base)
    target = resolve_delivery_target(runner.config.backend_options, 'agent-test', client.task_id)
    image = Path(target['path']) / '交付.md'
    image.write_text('synthetic result')
    sibling = delivery_base / 'unrelated.md'
    sibling.write_text('unrelated private file')
    executor.summary = result_body([image, sibling])
    uploads = []
    client.upload_task_artifact = lambda *args: uploads.append(args)
    client.fail_complete_before = True
    runner.process_once()
    assert len(uploads) == 1 and uploads[0][4] == str(image)
    receipt = runner.state.get_task_delivery('agent-test', client.task_id)
    assert receipt['artifact_delivery'] == target
    runner.config.backend_options['artifact_delivery_root'] = str(tmp_path / 'new-location')
    restored = restart(runner, client, executor)
    assert restored._artifact_delivery_target('agent-test', client.task_id) == target
    assert str(image.parent) in executor.calls[0][3]
    assert str(sibling) not in receipt['artifact_publication']['body']
    client.fail_complete_before = False
    restored.process_once()
    assert len(uploads) == len(executor.calls) == 1


@pytest.mark.parametrize('slug', ['../private', 'ok/../private', 'UPPER', ''])
def test_invalid_project_cannot_expand_delivery_scope(tmp_path, slug):
    with pytest.raises(ValueError):
        resolve_delivery_target({'artifact_delivery_root': str(tmp_path)}, slug, 1)


def test_delivery_target_rejects_mismatched_receipt_and_relative_base(tmp_path):
    with pytest.raises(ValueError):
        resolve_delivery_target({'artifact_delivery_root': 'relative'}, 'project', 1)
    with pytest.raises(ValueError):
        resolve_delivery_target({}, 'project', 2, frozen={'base': str(tmp_path), 'path': str(tmp_path/'project'/'task-1')})


def test_delivery_target_rejects_nested_directory_symlink(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    base = tmp_path / 'deliveries'
    base.mkdir()
    link = base / 'project'
    if os.name == 'nt':
        made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True)
        if made.returncode:
            pytest.skip('Host cannot create a directory junction')
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match='链接'):
            resolve_delivery_target({'artifact_delivery_root': str(base)}, 'project', 1)
        assert not (outside / 'task-1').exists()
    finally:
        os.rmdir(link) if os.name == 'nt' else link.unlink()


def test_unknown_upload_response_error_keeps_receipt_without_model_replay(repo, tmp_path):
    runner, client, executor = runtime(repo, tmp_path)
    (tmp_path / 'project' / 'report.md').write_text('synthetic')
    executor.summary = result_body(['report.md'])
    def fail_unknown(*args):
        raise ValueError('unknown response')
    client.upload_task_artifact = fail_unknown
    runner.process_once()
    assert repo.get_task(client.task_id)['status'] == 'in_progress'
    receipt = runner.state.get_task_delivery('agent-test', client.task_id)
    assert receipt['stage'] == 'agent_result' and not receipt.get('artifact_publish_failed')
    client.upload_task_artifact = lambda *args: None
    restart(runner, client, executor).process_once()
    assert len(executor.calls) == 1 and repo.get_task(client.task_id)['status'] == 'review'


def test_invalid_completion_response_is_bounded_and_retains_local_result(repo, tmp_path):
    from codex_bridge.client import HubClientError
    runner, client, executor = runtime(repo, tmp_path)
    client.upload_task_artifact = lambda *args: None
    attempts = []
    def reject(*args, **kwargs):
        attempts.append(kwargs)
        raise HubClientError('invalid completion', status_code=422)
    client.complete_task_execution = reject
    runner.process_once()
    restored = restart(runner, client, executor)
    restored.process_once()
    assert len(attempts) == 1 and len(executor.calls) == 1
    receipt = restored.state.get_task_delivery('agent-test', client.task_id)
    assert receipt['stage'] == 'agent_result_rejected' and receipt['result_summary']
    assert receipt['artifact_publication']['body'] and not receipt.get('artifact_publish_failed')


def test_unavailable_delivery_drive_reports_reason_before_any_execution(repo, tmp_path, monkeypatch):
    runner, client, executor = runtime(repo, tmp_path)
    def unavailable(*args, **kwargs):
        raise OSError('offline drive')
    monkeypatch.setattr('codex_bridge.delivery_target.resolve_delivery_target', unavailable)
    runner.process_once()
    assert not executor.calls and client.start_calls == 0
    task = repo.get_task(client.task_id)
    assert task['status'] == 'blocked' and '交付目录' in task['result_summary']
    restored = restart(runner, client, executor)
    restored.process_once()
    assert not executor.calls
