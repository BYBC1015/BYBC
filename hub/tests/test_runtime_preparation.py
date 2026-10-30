"""Real preparation and cancellation boundaries; no models or production tasks."""
from pathlib import Path
import threading

import pytest

from codex_bridge import attachments
from codex_bridge.agent_runtime import ExecutionRenewal
from codex_bridge.client import HubClientError
from test_agent_attachments import AttachmentClient, item, png
from test_agent_runtime import config_for
from test_execution_recovery import runtime


def prepare_twice(tmp_path, monkeypatch, *, image=False):
    config = config_for(tmp_path)
    data = png() if image else b'Latest approved brief'
    client = AttachmentClient([(item(data, name='source.png' if image else 'brief.txt'), data)])
    cache = attachments.AttachmentPreviewCache()
    name = '_image_input' if image else '_text_preview'
    original = getattr(attachments, name)
    parsed = []
    def parse(*args):
        parsed.append(args[0])
        return original(*args)
    monkeypatch.setattr(attachments, name, parse)
    if image:
        monkeypatch.setattr(attachments, 'MAX_IMAGE_PIXELS', 1)
        monkeypatch.setattr(attachments, 'IMAGE_INPUT_MAX_EDGE', 8)
    def prepare(task_id=42):
        return attachments.prepare_task_attachments(client, config, 'agent-test', task_id,
            config.projects['agent-test'], preview_cache=cache)
    return config, client, cache, parsed, prepare


@pytest.mark.parametrize('image', [False, True])
def test_second_preparation_rechecks_manifest_but_reuses_verified_preview(tmp_path, monkeypatch, image):
    _, client, _, parsed, prepare = prepare_twice(tmp_path, monkeypatch, image=image)
    first, second = prepare(), prepare()
    assert len(parsed) == 1 and client.manifest_calls == 2 and len(client.downloads) == 1
    assert first == second
    assert all(path.is_file() for path in second.images)


@pytest.mark.parametrize('change', ['modified', 'removed'])
@pytest.mark.parametrize('image', [False, True])
def test_missing_or_modified_derived_output_is_regenerated(tmp_path, monkeypatch, image, change):
    _, _, _, parsed, prepare = prepare_twice(tmp_path, monkeypatch, image=image)
    first = prepare()
    path = Path(first.files[0]['image_path' if image else 'text_preview_path'])
    expected = path.read_bytes()
    if change == 'removed':
        path.unlink()
    else:
        # Same length prevents a size-only validation from accidentally passing.
        path.write_bytes(b'X' * len(expected))
    restored = prepare()
    assert len(parsed) == 2 and path.read_bytes() == expected
    assert restored == first


def test_current_manifest_controls_reuse_removal_and_task_membership(tmp_path, monkeypatch):
    _, client, _, parsed, prepare = prepare_twice(tmp_path, monkeypatch)
    first = prepare()
    original = Path(first.files[0]['local_path'])
    original.write_bytes(b'local edit must not replace approved input')
    prepare()
    assert len(client.downloads) == 2 and len(parsed) == 1
    changed = b'Updated approved brief'
    client.files = [(item(changed, name='brief.txt'), changed)]
    second = prepare()
    assert len(parsed) == 2
    assert Path(second.files[0]['text_preview_path']).read_text(encoding='utf-8') == changed.decode()
    with pytest.raises(attachments.AttachmentPreparationError, match='其他任务'):
        prepare(43)
    client.files = []
    assert prepare().files == [] and len(parsed) == 2


def test_preview_cache_is_bounded_and_cannot_cross_task_paths(tmp_path, monkeypatch):
    config, client, _, parsed, _ = prepare_twice(tmp_path, monkeypatch)
    cache = attachments.AttachmentPreviewCache(max_entries=2)
    def prepare(task_id):
        client.files[0][0]['task_id'] = task_id
        return attachments.prepare_task_attachments(client, config, 'agent-test', task_id,
            config.projects['agent-test'], preview_cache=cache)
    paths = [prepare(task_id).files[0]['text_preview_path'] for task_id in (42, 43, 44)]
    assert len(set(paths)) == 3 and len(parsed) == 3
    prepare(42)
    assert len(parsed) == 4  # The first task was evicted, not silently trusted.


@pytest.mark.parametrize('cancel', [False, True])
def test_lease_renewal_covers_fresh_attachment_preparation_and_cancel_fences_launch(repo, tmp_path, monkeypatch, cancel):
    monkeypatch.setattr('codex_bridge.model_allocator.allocate_task_model', lambda *args: {})
    runner, client, executor = runtime(repo, tmp_path)
    renewals, entered = [], threading.Event()
    def renew(*args):
        entered.set()
        if cancel:
            raise HubClientError('revoked during preparation', status_code=409)
        return {}
    client.renew_task_execution = renew
    def renewal(*args):
        value = ExecutionRenewal(*args, interval=.01)
        renewals.append(value)
        return value
    monkeypatch.setattr('codex_bridge.runner.ExecutionRenewal', renewal)
    original = attachments.prepare_task_attachments
    calls = []
    def prepare(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            assert entered.wait(1), 'No lease contact during post-start input preparation'
            if cancel:
                assert renewals[0].cancelled.wait(1)
                repo.update_task_status(client.task_id, 'cancelled', 'Cancelled while inputs were preparing')
        return original(*args, **kwargs)
    monkeypatch.setattr('codex_bridge.runner.prepare_task_attachments', prepare)
    outcome = runner.process_once()
    assert len(calls) == 2 and not renewals[0].thread.is_alive()
    assert len(executor.calls) == (0 if cancel else 1)
    assert len(outcome.executed_codex) == (0 if cancel else 1)
    assert repo.get_task(client.task_id)['status'] == ('cancelled' if cancel else 'review')
