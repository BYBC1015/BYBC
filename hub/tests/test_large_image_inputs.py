import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image

from codex_bridge.attachments import AttachmentPreparationError, _image_input, prepare_task_attachments
from codex_bridge.runner import BridgeRunner, ProcessResult
from test_agent_attachments import AttachmentClient, ImageExecutor, item
from test_agent_runtime import config_for


def test_real_48mp_jpeg_reaches_executor_as_derivative_with_original_intact(tmp_path, monkeypatch):
    monkeypatch.setattr('codex_bridge.model_allocator.allocate_task_model', lambda *args: {})
    buffer = io.BytesIO()
    with Image.new('RGB', (15840, 3024), '#264f85') as image:
        image.save(buffer, 'JPEG')
    raw = buffer.getvalue()
    client = AttachmentClient([(item(raw, name='stage.jpg', content_type='image/jpeg'), raw)])
    config = config_for(tmp_path)
    executor = ImageExecutor()
    runner = BridgeRunner(config, client=client, executor=executor)
    runner._process_task({'id': 42, 'project_slug': 'agent-test', 'assigned_agent_id': 1,
                          'conversation_id': 5, 'status': 'open'}, 'test-device', ProcessResult())
    assert len(executor.calls) == 1
    assert len(executor.images) == 1
    with Image.open(executor.images[0]) as image:
        assert image.size == (4096, 782)
    original = next(config.projects['agent-test'].rglob('1-stage.jpg'))
    assert original.read_bytes() == raw
    prompt = executor.calls[0][3]
    assert json.dumps(str(original))[1:-1] in prompt and '自动适配' in prompt and '原图未修改' in prompt
    assert executor.images[0] != original


def test_transparent_large_image_and_concurrent_preparation_are_atomic(tmp_path, monkeypatch):
    monkeypatch.setattr('codex_bridge.attachments.MAX_IMAGE_PIXELS', 1000)
    monkeypatch.setattr('codex_bridge.attachments.IMAGE_INPUT_MAX_EDGE', 100)
    source = tmp_path / 'alpha.png'
    with Image.new('RGBA', (200, 100), (20, 40, 60, 80)) as image:
        image.save(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: _image_input(source, tmp_path), range(5)))
    assert len({str(path) for path, _ in results}) == 1
    with Image.open(results[0][0]) as result:
        assert result.size == (100, 50) and result.mode == 'RGBA'
        assert result.getpixel((50, 25))[3] == 80
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    assert not list(tmp_path.glob('*.part'))


def test_exif_orientation_and_first_frame_are_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr('codex_bridge.attachments.MAX_IMAGE_PIXELS', 1000)
    monkeypatch.setattr('codex_bridge.attachments.IMAGE_INPUT_MAX_EDGE', 100)
    source = tmp_path / 'rotated.jpg'
    with Image.new('RGB', (200, 100), 'blue') as image:
        exif = Image.Exif(); exif[274] = 6
        image.save(source, exif=exif)
    visual, note = _image_input(source, tmp_path)
    with Image.open(visual) as image:
        assert image.size == (50, 100)
        assert image.getexif().get(274, 1) == 1
    gif = tmp_path / 'animation.gif'
    with Image.new('RGB', (200, 100), 'red') as first, Image.new('RGB', (200, 100), 'blue') as second:
        first.save(gif, save_all=True, append_images=[second])
    visual, note = _image_input(gif, tmp_path)
    with Image.open(visual) as image:
        assert image.getpixel((20, 20))[0] > 240
    assert '第一帧' in note


def test_decode_limit_and_corruption_remain_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr('codex_bridge.attachments.MAX_IMAGE_PIXELS', 100)
    monkeypatch.setattr('codex_bridge.attachments.MAX_IMAGE_DECODE_PIXELS', 1000)
    source = tmp_path / 'too-large.png'
    with Image.new('RGB', (100, 100)) as image:
        image.save(source)
    original = source.read_bytes()
    with pytest.raises(AttachmentPreparationError, match='内存'):
        _image_input(source, tmp_path)
    assert source.read_bytes() == original and not list(tmp_path.glob('*.part'))
    source.write_bytes(b'not an image')
    with pytest.raises(AttachmentPreparationError, match='解码'):
        _image_input(source, tmp_path)


def test_derivative_failure_leaves_original_and_no_partial(tmp_path, monkeypatch):
    source = tmp_path / 'small.bmp'
    with Image.new('RGB', (30, 20)) as image:
        image.save(source)
    original = source.read_bytes()
    def fail(*args, **kwargs):
        raise OSError('write failed')
    monkeypatch.setattr(Image.Image, 'save', fail)
    with pytest.raises(AttachmentPreparationError):
        _image_input(source, tmp_path)
    assert source.read_bytes() == original
    assert list(tmp_path.iterdir()) == [source]


def test_locked_identical_derivative_reuses_verified_output(tmp_path, monkeypatch):
    source = tmp_path / 'small.bmp'
    with Image.new('RGB', (30, 20), 'red') as image:
        image.save(source)
    visual, _ = _image_input(source, tmp_path)
    def locked(*args):
        raise PermissionError('target is being read')
    monkeypatch.setattr(Path, 'replace', locked)
    assert _image_input(source, tmp_path)[0] == visual
    assert not list(tmp_path.glob('*.part'))
    visual.write_bytes(b'different unverified output')
    with pytest.raises(AttachmentPreparationError):
        _image_input(source, tmp_path)
