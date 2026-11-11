import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from codex_bridge import artifacts
from codex_bridge.artifacts import (
    ArtifactValidationError, collect_artifacts, preflight_artifacts,
    publish_artifacts, publish_artifacts_partial,
)
from codex_bridge.client import HubClientError


def manifest(*paths):
    return '<hub-artifacts>' + json.dumps([{'path': str(p)} for p in paths]) + '</hub-artifacts>'


def capture_client():
    calls = []
    return SimpleNamespace(upload_task_artifact=lambda *args: calls.append(args)), calls


def write(root, name, data=b'public result'):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_junit_rejection_still_delivers_maintenance_report(tmp_path):
    write(tmp_path, 'report.md')
    write(tmp_path, 'backend.xml', b'<testsuite tests="1"/>')
    body = '[报告](report.md)\n[JUnit](backend.xml)\n' + manifest('backend.xml', 'report.md')
    client, calls = capture_client()
    publication = publish_artifacts_partial(client, 'project', 37, {}, tmp_path, body)
    assert publication.published_count == 1
    assert [x[5] for x in calls] == ['report.md']
    assert calls[0][-1] == b'public result'
    assert [(x.code, x.name) for x in publication.issues] == [('unsupported_type', 'backend.xml')]
    assert 'ZIP' in publication.issue_summary()
    assert '[报告](report.md)' in publication.body
    assert '(backend.xml)' not in publication.body and '未发布' in publication.body
    assert 'hub-artifacts' not in publication.body


def test_strict_compatibility_does_not_start_upload_on_bad_manifest(tmp_path):
    write(tmp_path, 'report.md')
    write(tmp_path, 'backend.xml')
    client, calls = capture_client()
    body = manifest('report.md', 'backend.xml')
    with pytest.raises(ArtifactValidationError) as error:
        collect_artifacts(body, tmp_path)
    assert error.value.issues[0].code == 'unsupported_type'
    with pytest.raises(ArtifactValidationError):
        publish_artifacts(client, 'project', 1, {}, tmp_path, body)
    assert not calls


def test_private_and_outside_files_are_not_read_or_named_in_issues(tmp_path, monkeypatch):
    root = tmp_path / 'project'; root.mkdir()
    write(root, 'report.md')
    secret = write(root, '.private/hidden-token.md', b'secret')
    config = write(root, 'config.json', b'secret')
    outside = write(tmp_path, 'outside-credential.md', b'secret')
    old_open = Path.open
    def checked_open(self, *args, **kwargs):
        assert self not in {secret, config, outside}
        return old_open(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', checked_open)
    body = manifest(str(secret), str(config), str(outside), 'report.md')
    client, calls = capture_client()
    result = publish_artifacts_partial(client, 'project', 1, {}, root, body)
    assert result.published_count == 1 and len(calls) == 1
    assert {x.code for x in result.issues} == {'private_path', 'outside_root'}
    text = result.issue_summary()
    assert all(x not in text for x in ('hidden-token', 'config.json', 'outside-credential', str(tmp_path)))


def test_all_invalid_is_explicit_without_upload(tmp_path):
    client, calls = capture_client()
    result = publish_artifacts_partial(client, 'project', 1, {}, tmp_path,
        manifest('missing.md', 'test.xml', 'config.json'))
    assert not calls and result.published_count == 0
    assert {x.code for x in result.issues} == {'missing_file', 'unsupported_type', 'private_path'}


def test_rejected_private_file_alias_is_removed_from_body(tmp_path):
    config = write(tmp_path, 'config.json', b'secret')
    body = f'[文件](<{config.as_posix()}>)\n' + manifest('config.json')
    result = preflight_artifacts(body, tmp_path)
    assert not result.files and result.issues[0].code == 'private_path'
    assert str(tmp_path) not in result.body and 'config.json' not in result.body
    assert '未发布' in result.body


@pytest.mark.parametrize('bad', ['not-json', '{}', '[null,{}]', '[{"path":42}]'])
def test_invalid_manifest_does_not_hide_independent_markdown_report(tmp_path, bad):
    write(tmp_path, 'report.md')
    client, calls = capture_client()
    result = publish_artifacts_partial(client, 'project', 1, {}, tmp_path,
        '[报告](report.md)\n<hub-artifacts>' + bad + '</hub-artifacts>')
    assert result.published_count == 1 and len(calls) == 1
    assert result.issues and 'hub-artifacts' not in result.body


def test_manifest_with_invalid_member_preserves_other_valid_members(tmp_path):
    write(tmp_path, 'report.md')
    plan = preflight_artifacts('<hub-artifacts>[null,{"path":"report.md"}]</hub-artifacts>', tmp_path)
    assert len(plan.files) == 1 and plan.issues[0].code == 'invalid_manifest_item'


def test_extra_delivery_root_is_explicit_and_does_not_include_neighbour_task(tmp_path):
    root = tmp_path / 'workspace'; root.mkdir()
    delivery = tmp_path / 'Downloads/project/task-1'; delivery.mkdir(parents=True)
    approved = write(delivery, 'report.md')
    neighbour = write(delivery.parent / 'task-2', 'other.md')
    assert preflight_artifacts(manifest(str(approved)), root).issues[0].code == 'outside_root'
    plan = preflight_artifacts(manifest(str(approved), str(neighbour)), root, allowed_roots=(delivery,))
    assert [x['path'] for x in plan.files] == [approved]
    assert plan.issues[0].code == 'outside_root'
    client, calls = capture_client()
    result = publish_artifacts_partial(client, 'project', 1, {}, root,
        manifest(str(approved)), allowed_roots=(delivery,))
    assert result.published_count == 1 and len(calls) == 1


def test_absolute_relative_aliases_upload_once_with_original_key_identity(tmp_path):
    file = write(tmp_path, 'report.md')
    body = f'[A]({file.as_posix()})\n[B](report.md)\n' + manifest('report.md')
    client, calls = capture_client()
    result = publish_artifacts_partial(client, 'project', 1, {}, tmp_path, body)
    assert len(calls) == 1 and calls[0][4] == 'report.md'
    assert result.body.count('(report.md)') == 2


@pytest.mark.parametrize('status', [413, 415, 422])
def test_permanent_server_rejection_does_not_hide_later_valid_file(tmp_path, status):
    write(tmp_path, 'bad.png'); write(tmp_path, 'report.md')
    calls = []
    def upload(*args):
        calls.append(args)
        if args[5] == 'bad.png':
            raise HubClientError('private details must not be rendered', status_code=status)
    result = publish_artifacts_partial(SimpleNamespace(upload_task_artifact=upload), 'project', 1, {}, tmp_path,
        '[图](bad.png)\n[报告](report.md)\n' + manifest('bad.png', 'report.md'))
    assert len(calls) == 2 and result.published_count == 1
    assert result.issues[0].code == 'server_rejected'
    assert 'private details' not in result.issue_summary()
    assert '(bad.png)' not in result.body and '(report.md)' in result.body


@pytest.mark.parametrize('status', [401, 403, 404, 409, 429, 500, None])
def test_unknown_or_execution_fence_error_propagates_for_receipt_recovery(tmp_path, status):
    write(tmp_path, 'report.md')
    error = HubClientError('do not downgrade unknown delivery', status_code=status)
    def upload(*args):
        raise error
    with pytest.raises(HubClientError) as observed:
        publish_artifacts_partial(SimpleNamespace(upload_task_artifact=upload), 'project', 1, {}, tmp_path,
                                  manifest('report.md'))
    assert observed.value is error


def test_lost_upload_ack_reuses_stable_keys_and_bytes_on_retry(tmp_path):
    write(tmp_path, 'one.md'); write(tmp_path, 'two.md', b'two')
    stored, attempts = {}, []
    interrupted = False
    def upload(*args):
        nonlocal interrupted
        key, data = args[3], args[-1]
        assert stored.get(key, data) == data
        stored[key] = data
        attempts.append((args[5], key))
        if args[5] == 'two.md' and not interrupted:
            interrupted = True
            raise ConnectionError('server stored it, reply was lost')
    client = SimpleNamespace(upload_task_artifact=upload)
    body = manifest('one.md', 'two.md')
    with pytest.raises(ConnectionError):
        publish_artifacts_partial(client, 'project', 1, {}, tmp_path, body)
    result = publish_artifacts_partial(client, 'project', 1, {}, tmp_path, body)
    assert result.published_count == 2 and not result.issues
    assert len(stored) == 2 and attempts[:2] == attempts[2:]


def test_unknown_value_error_from_upload_is_not_a_local_validation_error(tmp_path):
    write(tmp_path, 'report.md')
    def upload(*args):
        raise ValueError('invalid response with unknown server outcome')
    with pytest.raises(ValueError, match='unknown server outcome'):
        publish_artifacts_partial(SimpleNamespace(upload_task_artifact=upload), 'project', 1, {}, tmp_path,
                                  manifest('report.md'))


def test_changed_file_after_preflight_is_not_uploaded(tmp_path, monkeypatch):
    file = write(tmp_path, 'report.md')
    plan = preflight_artifacts(manifest('report.md'), tmp_path)
    file.write_bytes(b'replaced after preflight')
    monkeypatch.setattr(artifacts, 'preflight_artifacts', lambda *args, **kwargs: plan)
    client, calls = capture_client()
    result = publish_artifacts_partial(client, 'project', 1, {}, tmp_path, '')
    assert not calls and result.issues[0].code == 'changed_file'


def test_existing_file_overwritten_before_preflight_is_valid(tmp_path):
    file = write(tmp_path, 'report.md', b'old')
    file.write_bytes(b'new delivery')
    # A real Windows stat/fstat pair can disagree on ctime for this file. An
    # explicitly different mtime makes that discrepancy deterministic.
    os.utime(file, (1_700_000_000, 1_700_000_000))
    client, calls = capture_client()
    result = publish_artifacts_partial(client, 'project', 1, {}, tmp_path, manifest('report.md'))
    assert result.published_count == 1 and not result.issues
    assert calls[0][-1] == b'new delivery'


def test_actual_opened_file_outside_authorization_is_rejected(tmp_path, monkeypatch):
    write(tmp_path, 'report.md')
    monkeypatch.setattr(artifacts, '_opened_path', lambda *_: tmp_path.parent / 'private.md')
    client, calls = capture_client()
    result = publish_artifacts_partial(client, 'project', 1, {}, tmp_path, manifest('report.md'))
    assert not calls and result.issues[0].code == 'changed_file'


def test_file_limit_does_not_consume_budget_for_rejected_files(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, 'MAX_FILE_BYTES', 8)
    monkeypatch.setattr(artifacts, 'MAX_TOTAL_BYTES', 9)
    write(tmp_path, 'too-big.txt', b'x' * 9)
    write(tmp_path, 'one.txt', b'12345')
    write(tmp_path, 'two.txt', b'12345')
    write(tmp_path, 'small.txt', b'1234')
    plan = preflight_artifacts(manifest('too-big.txt', 'one.txt', 'two.txt', 'small.txt'), tmp_path)
    assert [x['path'].name for x in plan.files] == ['one.txt', 'small.txt']
    assert {x.code for x in plan.issues} == {'file_too_large', 'delivery_limit'}


def test_existing_hub_links_and_remote_links_are_preserved(tmp_path):
    body = '[任务](../tasks/37) [网页](https://example.invalid/report.md)'
    plan = preflight_artifacts(body, tmp_path)
    assert plan.body == body and not plan.issues and not plan.files


def test_implicit_deliverable_over_total_limit_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, 'MAX_TOTAL_BYTES', 9)
    write(tmp_path, 'one.md', b'12345'); write(tmp_path, 'two.md', b'12345')
    plan = preflight_artifacts('[A](one.md) [B](two.md)', tmp_path)
    assert len(plan.files) == 1 and plan.issues[0].code == 'delivery_limit'
    assert '(two.md)' not in plan.body


def test_directory_link_escape_does_not_upload_outside_file(tmp_path):
    root = tmp_path / 'project'; root.mkdir()
    outside = tmp_path / 'private'; outside.mkdir()
    write(outside, 'secret.md', b'secret')
    link = root / 'linked'
    if os.name == 'nt':
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True)
        if result.returncode:
            pytest.skip('Host cannot create a directory junction')
    else:
        link.symlink_to(outside, target_is_directory=True)
    client, calls = capture_client()
    try:
        result = publish_artifacts_partial(client, 'project', 1, {}, root, manifest('linked/secret.md'))
        assert not calls and result.issues[0].code == 'outside_root'
    finally:
        if os.name == 'nt':
            os.rmdir(link)
        else:
            link.unlink()
