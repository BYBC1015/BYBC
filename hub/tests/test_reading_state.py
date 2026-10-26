import pytest

from codex_hub.reading_state import (
    LongContentPolicy,
    ReaderKey,
    ReadingStateStore,
    describe_markdown_entry,
    initialize_reading_state_schema,
    long_content_path,
)


def _seed_reading(repo):
    owner = repo.create_owner_if_missing("reader", "secret")
    device = repo.create_device("reader-device")
    other_device = repo.create_device("other-reader-device")
    project = repo.create_project(
        slug="reader room",
        name="Reader room",
        created_by_device_id=device["id"],
    )
    other_project = repo.create_project(
        slug="other",
        name="Other",
        created_by_device_id=other_device["id"],
    )
    first = repo.create_entry(
        project_id=project["id"],
        author_device_id=device["id"],
        source_label=device["label"],
        kind="markdown",
        body_markdown="# First\n" + "long paragraph " * 20,
    )
    second = repo.create_entry(
        project_id=project["id"],
        author_device_id=device["id"],
        source_label=device["label"],
        kind="markdown",
        body_markdown="# Second\nMore content",
    )
    third = repo.create_entry(
        project_id=project["id"],
        author_device_id=device["id"],
        source_label=device["label"],
        kind="message",
        body_markdown="Third message",
    )
    other_entry = repo.create_entry(
        project_id=other_project["id"],
        author_device_id=other_device["id"],
        source_label=other_device["label"],
        kind="markdown",
        body_markdown="Other project",
    )
    image = repo.create_attachment(
        project_id=project["id"],
        entry_id=first["id"],
        original_filename="last-frame.webp",
        stored_relpath="reading/last-frame.webp",
        content_type="image/webp",
        byte_size=100,
        sha256="a" * 64,
        created_by_device_id=device["id"],
    )
    video = repo.create_attachment(
        project_id=project["id"],
        entry_id=second["id"],
        original_filename="sequence.mp4",
        stored_relpath="reading/sequence.mp4",
        content_type="video/mp4",
        byte_size=200,
        sha256="b" * 64,
        created_by_device_id=device["id"],
    )
    document = repo.create_attachment(
        project_id=project["id"],
        entry_id=third["id"],
        original_filename="notes.docx",
        stored_relpath="reading/notes.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        byte_size=300,
        sha256="c" * 64,
        created_by_device_id=device["id"],
    )
    foreign_image = repo.create_attachment(
        project_id=other_project["id"],
        entry_id=other_entry["id"],
        original_filename="foreign.png",
        stored_relpath="reading/foreign.png",
        content_type="image/png",
        byte_size=100,
        sha256="d" * 64,
        created_by_device_id=other_device["id"],
    )
    return {
        "owner": owner,
        "device": device,
        "other_device": other_device,
        "project": project,
        "other_project": other_project,
        "first": first,
        "second": second,
        "third": third,
        "other_entry": other_entry,
        "image": image,
        "video": video,
        "document": document,
        "foreign_image": foreign_image,
    }


def test_reading_schema_is_idempotent_and_tracks_initial_unread_boundary(repo, db):
    seeded = _seed_reading(repo)
    initialize_reading_state_schema(db)
    initialize_reading_state_schema(db)
    reader = ReaderKey("user", seeded["owner"]["id"])

    snapshot = ReadingStateStore(db).snapshot(seeded["project"]["id"], reader)

    assert snapshot["continue_reading"] is None
    assert snapshot["last_image_attachment_id"] is None
    assert snapshot["unread"] == {
        "last_read_entry_id": None,
        "latest_entry_id": seeded["third"]["id"],
        "unread_count": 3,
        "has_unread": True,
        "first_unread_entry_id": seeded["first"]["id"],
        "divider_before_entry_id": seeded["first"]["id"],
    }


def test_last_image_and_continue_reading_are_project_scoped(repo, db):
    seeded = _seed_reading(repo)
    initialize_reading_state_schema(db)
    reader = ReaderKey("user", seeded["owner"]["id"])
    store = ReadingStateStore(db)

    snapshot = store.remember_image(
        seeded["project"]["id"],
        reader,
        seeded["image"]["id"],
        mark_entry_read=True,
    )

    assert snapshot["last_image_attachment_id"] == seeded["image"]["id"]
    assert snapshot["continue_reading"]["kind"] == "image"
    assert snapshot["continue_reading"]["href"].startswith(
        "/projects/reader%20room?media="
    )
    assert snapshot["unread"]["first_unread_entry_id"] == seeded["second"]["id"]

    with pytest.raises(ValueError, match="not an image"):
        store.remember_image(seeded["project"]["id"], reader, seeded["document"]["id"])
    with pytest.raises(ValueError, match="does not belong"):
        store.remember_image(
            seeded["project"]["id"], reader, seeded["foreign_image"]["id"]
        )


def test_markdown_positions_are_kept_per_entry_and_long_content_has_own_link(repo, db):
    seeded = _seed_reading(repo)
    initialize_reading_state_schema(db)
    reader = ReaderKey("user", seeded["owner"]["id"])
    store = ReadingStateStore(
        db, long_content_policy=LongContentPolicy(min_characters=100, min_lines=99)
    )

    first_position = store.save_markdown_position(
        seeded["project"]["id"],
        reader,
        seeded["first"]["id"],
        anchor="section-2",
        offset=480,
        progress=0.4,
    )
    store.save_markdown_position(
        seeded["project"]["id"],
        reader,
        seeded["second"]["id"],
        offset=20,
        progress=0.1,
    )

    assert first_position["anchor"] == "section-2"
    assert first_position["offset"] == 480
    assert first_position["progress"] == 0.4
    assert (
        store.get_markdown_position(
            seeded["project"]["id"], reader, seeded["first"]["id"]
        )["offset"]
        == 480
    )
    snapshot = store.snapshot(seeded["project"]["id"], reader)
    assert snapshot["last_markdown_entry_id"] == seeded["second"]["id"]

    store.save_markdown_position(
        seeded["project"]["id"], reader, seeded["first"]["id"], progress=0.5
    )
    continuation = store.continue_reading(seeded["project"]["id"], reader)
    assert continuation["is_long_content"] is True
    assert continuation["href"] == long_content_path(
        seeded["project"]["slug"], seeded["first"]["id"]
    )

    with pytest.raises(ValueError, match="between 0 and 1"):
        store.save_markdown_position(
            seeded["project"]["id"], reader, seeded["first"]["id"], progress=1.1
        )
    with pytest.raises(ValueError, match="does not belong"):
        store.save_markdown_position(
            seeded["project"]["id"], reader, seeded["other_entry"]["id"]
        )


def test_video_progress_resumes_per_video_and_detects_completion(repo, db):
    seeded = _seed_reading(repo)
    initialize_reading_state_schema(db)
    reader = ReaderKey("user", seeded["owner"]["id"])
    store = ReadingStateStore(db)

    progress = store.save_video_progress(
        seeded["project"]["id"],
        reader,
        seeded["video"]["id"],
        position_seconds=25,
        duration_seconds=100,
    )
    assert progress["offset"] == 25
    assert progress["duration"] == 100
    assert progress["progress"] == 0.25
    assert progress["completed"] is False

    completed = store.save_video_progress(
        seeded["project"]["id"],
        reader,
        seeded["video"]["id"],
        position_seconds=99.5,
    )
    assert completed["progress"] == 1.0
    assert completed["completed"] is True
    continuation = store.continue_reading(seeded["project"]["id"], reader)
    assert continuation["kind"] == "video"
    assert continuation["position"]["completed"] is True

    with pytest.raises(ValueError, match="not a video"):
        store.save_video_progress(
            seeded["project"]["id"],
            reader,
            seeded["document"]["id"],
            position_seconds=1,
        )


def test_unread_boundary_never_moves_backwards_and_readers_are_isolated(repo, db):
    seeded = _seed_reading(repo)
    initialize_reading_state_schema(db)
    user_reader = ReaderKey("user", seeded["owner"]["id"])
    device_reader = ReaderKey("device", seeded["device"]["id"])
    store = ReadingStateStore(db)

    unread = store.mark_read_through(
        seeded["project"]["id"], user_reader, seeded["second"]["id"]
    )
    assert unread["unread_count"] == 1
    assert unread["divider_before_entry_id"] == seeded["third"]["id"]

    store.mark_read_through(seeded["project"]["id"], user_reader, seeded["first"]["id"])
    assert (
        store.unread_status(seeded["project"]["id"], user_reader)["last_read_entry_id"]
        == seeded["second"]["id"]
    )
    assert (
        store.unread_status(seeded["project"]["id"], device_reader)["unread_count"] == 3
    )

    all_read = store.mark_read_through(seeded["project"]["id"], user_reader)
    assert all_read["unread_count"] == 0
    assert all_read["divider_before_entry_id"] is None


def test_long_content_description_only_links_long_markdown():
    policy = LongContentPolicy(min_characters=10, min_lines=10)

    short = describe_markdown_entry("slug", {"id": 1, "body_markdown": "short"}, policy)
    long = describe_markdown_entry(
        "slug with space", {"id": 2, "body_markdown": "long enough value"}, policy
    )

    assert short == {"entry_id": 1, "is_long_content": False, "long_content_url": None}
    assert long["long_content_url"] == "/projects/slug%20with%20space/entries/2"


def test_reader_key_rejects_invalid_identity():
    with pytest.raises(ValueError, match="reader_type"):
        ReaderKey("anonymous", 1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive"):
        ReaderKey("user", 0)
