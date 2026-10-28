import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image

from codex_hub import media_assets


def stored_image(tmp_path, image, *, name="source.png", **save_options):
    path = tmp_path / name
    image.save(path, **save_options)
    return path, {
        "id": 1,
        "original_filename": name,
        "stored_relpath": name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def test_jpeg_preview_reduces_before_orientation_and_keeps_original(tmp_path, monkeypatch):
    exif = Image.Exif()
    exif[274] = 6
    with Image.new("RGB", (4000, 2000), "red") as source:
        original, attachment = stored_image(tmp_path, source, name="source.jpg", exif=exif)
    before = original.read_bytes()
    transpose = media_assets.ImageOps.exif_transpose
    copied_sizes = []

    def bounded_transpose(image):
        copied_sizes.append(image.size)
        return transpose(image)

    monkeypatch.setattr(media_assets.ImageOps, "exif_transpose", bounded_transpose)
    derived = media_assets.thumbnail_path(tmp_path, attachment, "thumb")

    assert copied_sizes == [(360, 180)]
    with Image.open(derived) as preview:
        assert preview.size == (180, 360)
        assert preview.getexif().get(274, 1) == 1
    assert original.read_bytes() == before
    assert media_assets.thumbnail_path(tmp_path, attachment, "thumb") == derived
    assert len(copied_sizes) == 1  # Subsequent requests reuse a complete preview.


@pytest.mark.parametrize("mode", ["RGBA", "LA", "P"])
def test_image_preview_preserves_transparency(tmp_path, mode):
    with Image.new(mode, (64, 40)) as source:
        if mode == "P":
            source.putpalette([255, 0, 0, 0, 0, 255] + [0] * 762)
            source.paste(1, (32, 0, 64, 40))
            source.info["transparency"] = 0
        elif mode == "LA":
            source.paste((180, 128), (32, 0, 64, 40))
        else:
            source.paste((255, 0, 0, 128), (32, 0, 64, 40))
        original, attachment = stored_image(tmp_path, source)
    before = original.read_bytes()

    derived = media_assets.thumbnail_path(tmp_path, attachment, "preview")

    with Image.open(derived) as preview:
        assert preview.mode == "RGBA"
        assert preview.getpixel((10, 10))[3] == 0
        assert preview.getpixel((50, 10))[3] == (255 if mode == "P" else 128)
    assert original.read_bytes() == before


def test_extreme_image_does_not_break_metadata_or_preview_and_guard_stays_enabled(tmp_path, monkeypatch):
    with Image.new("RGB", (20, 20), "red") as source:
        original, attachment = stored_image(tmp_path, source)
    before = original.read_bytes()
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)

    assert media_assets.image_dimensions(original) == (None, None)
    decorated = media_assets.decorate_attachment(attachment, files_root=tmp_path)
    assert decorated["width"] is None and decorated["height"] is None
    assert media_assets.thumbnail_path(tmp_path, attachment, "preview") is None
    assert Image.MAX_IMAGE_PIXELS == 100
    assert original.read_bytes() == before
    assert not list(tmp_path.rglob("*.webp"))


def test_failed_preview_write_removes_partial_and_preserves_original(tmp_path, monkeypatch):
    with Image.new("RGB", (20, 20), "red") as source:
        original, attachment = stored_image(tmp_path, source)
    before = original.read_bytes()

    def failed_save(self, path, *args, **kwargs):
        path.write_bytes(b"partial preview")
        raise OSError("Disk write failed")

    monkeypatch.setattr(Image.Image, "save", failed_save)
    assert media_assets.thumbnail_path(tmp_path, attachment, "preview") is None
    assert original.read_bytes() == before
    assert not list(tmp_path.rglob("*.webp"))


def test_simultaneous_preview_requests_publish_only_complete_files(tmp_path, monkeypatch):
    with Image.new("RGB", (80, 40), "blue") as source:
        original, attachment = stored_image(tmp_path, source)
    before = original.read_bytes()
    real_save = Image.Image.save
    barrier = threading.Barrier(2)
    writes = []
    lock = threading.Lock()

    def concurrent_save(self, path, *args, **kwargs):
        with lock:
            writes.append(path)
        barrier.wait(timeout=10)
        return real_save(self, path, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", concurrent_save)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(media_assets.thumbnail_path, tmp_path, attachment, "preview") for _ in range(2)]
        paths = [job.result(timeout=15) for job in jobs]

    assert paths[0] is not None and paths[0] == paths[1]
    assert len(set(writes)) == 2
    with Image.open(paths[0]) as preview:
        preview.load()
        assert preview.size == (80, 40)
    assert list(tmp_path.rglob("*.webp")) == [paths[0]]
    assert original.read_bytes() == before


def test_corrupt_image_keeps_original_and_returns_no_preview(tmp_path):
    original = tmp_path / "corrupt.png"
    original.write_bytes(b"not an image")
    attachment = {"id": 1, "original_filename": original.name, "stored_relpath": original.name}

    assert media_assets.image_dimensions(original) == (None, None)
    assert media_assets.thumbnail_path(tmp_path, attachment, "preview") is None
    assert original.read_bytes() == b"not an image"
    assert not list(tmp_path.rglob("*.webp"))


def test_preview_reuses_concurrent_winner_when_windows_refuses_replacement(tmp_path, monkeypatch):
    with Image.new("RGB", (80, 40), "blue") as source:
        original, attachment = stored_image(tmp_path, source)
    before = original.read_bytes()

    def already_published(path, target):
        target.write_bytes(path.read_bytes())
        raise PermissionError("Another reader has the completed target open")

    monkeypatch.setattr(Path, "replace", already_published)
    derived = media_assets.thumbnail_path(tmp_path, attachment, "preview")

    assert derived is not None
    with Image.open(derived) as preview:
        preview.load()
        assert preview.size == (80, 40)
    assert list(tmp_path.rglob("*.webp")) == [derived]
    assert original.read_bytes() == before
