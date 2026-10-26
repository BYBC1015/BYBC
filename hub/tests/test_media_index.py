import json

import pytest

from codex_hub.media_index import (
    MediaFilters,
    MediaIndex,
    classify_attachment,
    initialize_media_indexes,
    parse_generation_metadata,
)


def _seed_media(repo, db):
    device_a = repo.create_device("render-node-a")
    device_b = repo.create_device("camera-b")
    project = repo.create_project(
        slug="media room",
        name="Media room",
        created_by_device_id=device_a["id"],
    )
    image_entry = repo.create_entry(
        project_id=project["id"],
        author_device_id=device_a["id"],
        source_label=device_a["label"],
        kind="message",
        body_markdown="First generated skyline",
        metadata_json=json.dumps(
            {
                "generation": {
                    "batch_id": "batch-alpha",
                    "prompt": "A quiet skyline at blue hour",
                    "model": "image-model-v2",
                    "resolution": "1536x1024",
                    "params": {"seed": 42, "steps": 30},
                }
            }
        ),
    )
    image = repo.create_attachment(
        project_id=project["id"],
        entry_id=image_entry["id"],
        original_filename="skyline.png",
        stored_relpath="media/skyline.png",
        content_type="application/octet-stream",
        byte_size=1200,
        sha256="1" * 64,
        created_by_device_id=device_a["id"],
    )
    video_entry = repo.create_entry(
        project_id=project["id"],
        author_device_id=device_b["id"],
        source_label=device_b["label"],
        kind="message",
        body_markdown="Camera movement test",
        metadata_json=json.dumps(
            {
                "media": {
                    "generation_batch": "batch-beta",
                    "generation_prompt": "Slow dolly toward the door",
                    "model_name": "video-model-v1",
                    "dimensions": {"width": 1920, "height": 1080},
                    "parameters": {"duration": 6},
                }
            }
        ),
    )
    video = repo.create_attachment(
        project_id=project["id"],
        entry_id=video_entry["id"],
        original_filename="dolly.MOV",
        stored_relpath="media/dolly.mov",
        content_type="video/quicktime; charset=binary",
        byte_size=3400,
        sha256="2" * 64,
        created_by_device_id=device_b["id"],
    )
    document_entry = repo.create_entry(
        project_id=project["id"],
        author_device_id=device_a["id"],
        source_label=device_a["label"],
        kind="message",
        body_markdown="Final production brief",
        metadata_json=json.dumps(
            {"prompt": "must not be displayed for documents", "batch_id": "batch-alpha"}
        ),
    )
    document = repo.create_attachment(
        project_id=project["id"],
        entry_id=document_entry["id"],
        original_filename="brief.pdf",
        stored_relpath="docs/brief.pdf",
        content_type="application/pdf",
        byte_size=5600,
        sha256="3" * 64,
        created_by_device_id=device_a["id"],
    )
    db.execute(
        "UPDATE attachments SET created_at = ? WHERE id = ?",
        ("2026-07-01T08:00:00+00:00", image["id"]),
    )
    db.execute(
        "UPDATE attachments SET created_at = ? WHERE id = ?",
        ("2026-07-02T08:00:00+00:00", video["id"]),
    )
    db.execute(
        "UPDATE attachments SET created_at = ? WHERE id = ?",
        ("2026-07-03T08:00:00+00:00", document["id"]),
    )
    db.commit()
    return {
        "project": project,
        "device_a": device_a,
        "device_b": device_b,
        "image_entry": image_entry,
        "video_entry": video_entry,
        "document_entry": document_entry,
        "image": image,
        "video": video,
        "document": document,
    }


def test_attachment_classification_uses_mime_then_extension():
    assert classify_attachment("frame.bin", "image/webp") == "image"
    assert classify_attachment("clip.MP4", "application/octet-stream") == "video"
    assert (
        classify_attachment("proposal.docx", "application/octet-stream") == "document"
    )
    assert classify_attachment("deck.pdf", "application/pdf") == "document"


def test_generation_metadata_accepts_nested_and_per_attachment_values():
    metadata = {
        "generation": {"prompt": "shared", "model": "base", "size": "512x768"},
        "attachments": {
            "9": {
                "prompt": "specific",
                "batch_id": "batch-9",
                "parameters": {"seed": 9},
            }
        },
    }

    parsed = parse_generation_metadata(metadata, attachment_id=9, filename="frame.png")

    assert parsed == {
        "prompt": "specific",
        "width": 512,
        "height": 768,
        "dimensions": "512\u00d7768",
        "model": "base",
        "parameters": {"seed": 9},
        "batch_id": "batch-9",
    }
    assert parse_generation_metadata("not-json") == {}


def test_media_index_categorizes_and_exposes_optional_generation_fields(repo, db):
    seeded = _seed_media(repo, db)
    initialize_media_indexes(db)

    items = MediaIndex(db).list_items(seeded["project"]["id"])
    by_category = {item["category"]: item for item in items}

    image = by_category["image"]
    assert image["prompt"] == "A quiet skyline at blue hour"
    assert image["dimensions"] == "1536\u00d71024"
    assert image["model"] == "image-model-v2"
    assert image["parameters"] == {"seed": 42, "steps": 30}
    assert image["generation_batch"] == "batch-alpha"
    assert image["device_label"] == "render-node-a"
    assert image["message_excerpt"] == "First generated skyline"
    assert image["preview_kind"] == "image"
    assert image["download_only"] is False
    assert image["download_url"].startswith("/projects/media%20room/attachments/")

    document = by_category["document"]
    assert document["preview_kind"] is None
    assert document["preview_url"] is None
    assert document["download_only"] is True
    assert document["generation"] == {}
    assert document["prompt"] is None
    assert document["model"] is None

    index_names = {
        row["name"] for row in db.execute("PRAGMA index_list(attachments)").fetchall()
    }
    assert "idx_attachments_project_created" in index_names


def test_media_index_filters_by_message_time_device_batch_and_text(repo, db):
    seeded = _seed_media(repo, db)
    index = MediaIndex(db)
    project_id = seeded["project"]["id"]

    assert [
        item["id"]
        for item in index.list_items(
            project_id, MediaFilters(message_id=seeded["image_entry"]["id"])
        )
    ] == [seeded["image"]["id"]]
    assert [
        item["id"]
        for item in index.list_items(
            project_id, MediaFilters(message_query="movement test")
        )
    ] == [seeded["video"]["id"]]
    assert [
        item["id"]
        for item in index.list_items(
            project_id,
            MediaFilters(
                created_from="2026-07-02T00:00:00+00:00",
                created_to="2026-07-02T23:59:59+00:00",
            ),
        )
    ] == [seeded["video"]["id"]]
    assert [
        item["id"]
        for item in index.list_items(
            project_id, MediaFilters(device_id=seeded["device_b"]["id"])
        )
    ] == [seeded["video"]["id"]]
    assert [
        item["id"]
        for item in index.list_items(
            project_id, MediaFilters(generation_batch="batch-alpha")
        )
    ] == [seeded["image"]["id"]]
    assert [
        item["id"]
        for item in index.list_items(project_id, MediaFilters(category="document"))
    ] == [seeded["document"]["id"]]

    assert index.category_counts(project_id) == {"image": 1, "video": 1, "document": 1}


def test_media_index_rejects_invalid_filters(repo, db):
    seeded = _seed_media(repo, db)
    index = MediaIndex(db)

    with pytest.raises(ValueError, match="category"):
        index.list_items(
            seeded["project"]["id"],
            MediaFilters(category="audio"),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="created_from"):
        index.list_items(
            seeded["project"]["id"],
            MediaFilters(
                created_from="2026-08-01T00:00:00+00:00",
                created_to="2026-07-01T00:00:00+00:00",
            ),
        )
