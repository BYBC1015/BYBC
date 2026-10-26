from codex_hub.reading import (
    format_byte_size,
    is_video_attachment,
    normalized_content_type,
    video_attachment_view,
    video_content_type,
)


def test_video_classification_is_conservative():
    assert is_video_attachment(
        {"original_filename": "demo.bin", "content_type": "video/mp4; charset=binary"}
    )
    assert is_video_attachment(
        {"original_filename": "camera.WEBM", "content_type": "application/octet-stream"}
    )
    assert not is_video_attachment(
        {"original_filename": "trap.mp4", "content_type": "text/html"}
    )


def test_office_and_pdf_remain_ordinary_attachments():
    for filename, content_type in (
        ("brief.pdf", "application/pdf"),
        (
            "proposal.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        ("forecast.xlsx", "application/vnd.ms-excel"),
    ):
        attachment = {"original_filename": filename, "content_type": content_type}
        assert not is_video_attachment(attachment)
        assert video_attachment_view(attachment, source_url="/attachment/1") is None

    assert not is_video_attachment(
        {"original_filename": "mislabelled.pdf", "content_type": "video/mp4"}
    )


def test_video_view_contains_only_lazy_reader_metadata():
    attachment = {
        "id": 42,
        "original_filename": "walkthrough.mp4",
        "content_type": "application/octet-stream",
        "byte_size": 3 * 1024 * 1024,
    }
    view = video_attachment_view(
        attachment,
        source_url="/projects/demo/attachments/42?inline=1",
        poster_url="/static/posters/42.jpg",
    )

    assert view == {
        "id": "42",
        "filename": "walkthrough.mp4",
        "content_type": "video/mp4",
        "byte_size": 3 * 1024 * 1024,
        "size_label": "3.0 MB",
        "source_url": "/projects/demo/attachments/42?inline=1",
        "poster_url": "/static/posters/42.jpg",
    }


def test_content_type_and_size_normalization():
    assert normalized_content_type(" Video/QuickTime ; charset=x ") == "video/quicktime"
    assert video_content_type(
        {"original_filename": "clip.mov", "content_type": "application/octet-stream"}
    ) == "video/quicktime"
    assert format_byte_size(0) == "0 B"
    assert format_byte_size(1536) == "1.5 KB"
