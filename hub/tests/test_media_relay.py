from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import pytest
from fastapi.testclient import TestClient

from codex_hub import media_relay
from codex_hub.main import create_app


@pytest.fixture
def relay_asset(client, repo):
    owner = repo.create_device(label="media-relay-writer")
    token = repo.create_api_token(device_id=owner["id"], name="media-relay")
    project = repo.create_project(slug="relay-files", name="Relay files", created_by_device_id=owner["id"])
    content = bytes(range(256)) * 40
    headers = {"Authorization": f"Bearer {token.raw}"}
    upload = client.post("/api/projects/relay-files/attachments", headers=headers,
                         files={"file": ("scene.mp4", content, "video/mp4")})
    assert upload.status_code == 201
    attachment = upload.json()["attachment"]
    return {"project": project, "owner": owner, "token": token.raw, "headers": headers,
            "attachment": attachment, "content": content,
            "share_path": f"/api/projects/relay-files/attachments/{attachment['id']}/share-link",
            "private_path": f"/api/projects/relay-files/attachments/{attachment['id']}"}


def share(client, asset, seconds=86400, headers=None):
    response = client.post(asset["share_path"], headers=headers or asset["headers"],
                           json={"expires_in_seconds": seconds})
    assert response.status_code == 200, response.text
    return response.json()


def altered_url(url, **updates):
    parts = urlsplit(url)
    query = {key: values[0] for key, values in parse_qs(parts.query).items()}
    query.update(updates)
    return urlunsplit(parts._replace(query=urlencode(query)))


def test_share_creation_requires_device_token_and_project_write(client, repo, relay_asset):
    assert client.post(relay_asset["share_path"], json={}).status_code == 401
    for permission in (None, "read"):
        device = repo.create_device(label=f"media-observer-{permission}")
        token = repo.create_api_token(device["id"], "observer")
        if permission:
            repo.add_project_member(relay_asset["project"]["id"], device["id"], permission)
        response = client.post(relay_asset["share_path"], json={},
                               headers={"Authorization": f"Bearer {token.raw}"})
        assert response.status_code == 403
    assert share(client, relay_asset)["url"]


def test_attachment_must_belong_to_writable_project(client, repo, relay_asset):
    repo.create_project(slug="other-files", name="Other", created_by_device_id=relay_asset["owner"]["id"])
    wrong_path = relay_asset["share_path"].replace("relay-files", "other-files")
    assert client.post(wrong_path, headers=relay_asset["headers"], json={}).status_code == 404


@pytest.mark.parametrize("seconds", [0, -1, 86401, True, "86400", 1.5, None])
def test_expiry_requires_integer_with_one_day_maximum(client, relay_asset, seconds):
    response = client.post(relay_asset["share_path"], headers=relay_asset["headers"],
                           json={"expires_in_seconds": seconds})
    assert response.status_code == 422


def test_expiry_defaults_to_one_day_and_returns_no_device_secret(client, relay_asset, monkeypatch):
    monkeypatch.setattr(media_relay.time, "time", lambda: 1800000000)
    response = client.post(relay_asset["share_path"], headers=relay_asset["headers"], json={})
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"url", "expires_at"}
    assert parse_qs(urlsplit(data["url"]).query)["expires"] == ["1800086400"]
    assert data["expires_at"].endswith("Z")
    assert relay_asset["token"] not in response.text
    assert "Authorization" not in response.text
    assert client.get(data["url"]).status_code == 200


def test_public_get_head_range_and_private_link_auth_are_separate(client, relay_asset):
    url = share(client, relay_asset)["url"]
    full = client.get(url)
    assert full.status_code == 200
    assert full.content == relay_asset["content"]
    assert full.headers["content-type"] == "video/mp4"
    assert full.headers["content-length"] == str(len(relay_asset["content"]))
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-encoding"] == "identity"
    assert full.headers["cache-control"] == "private, no-store"
    head = client.head(url)
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == str(len(relay_asset["content"]))
    for header, expected, content_range in [
        ("bytes=2-9", relay_asset["content"][2:10], "bytes 2-9/10240"),
        ("bytes=-4", relay_asset["content"][-4:], "bytes 10236-10239/10240"),
        ("bytes=10236-", relay_asset["content"][-4:], "bytes 10236-10239/10240"),
    ]:
        response = client.get(url, headers={"Range": header})
        assert response.status_code == 206
        assert response.content == expected
        assert response.headers["content-range"] == content_range
        assert response.headers["content-length"] == str(len(expected))
    invalid_range = client.get(url, headers={"Range": "bytes=30000-"})
    assert invalid_range.status_code == 416
    # Creating a signed URL does not relax either existing authenticated endpoint.
    assert client.get(relay_asset["private_path"]).status_code == 401
    assert client.get(relay_asset["private_path"], headers=relay_asset["headers"]).status_code == 200
    web_path = relay_asset["private_path"].replace("/api/projects/", "/projects/")
    web_response = client.get(web_path, follow_redirects=False)
    assert web_response.status_code == 303
    assert "/login" in web_response.headers["location"]


def test_expired_links_and_head_are_denied(client, relay_asset, monkeypatch):
    monkeypatch.setattr(media_relay.time, "time", lambda: 1800000000)
    url = share(client, relay_asset, seconds=1)["url"]
    assert client.get(url).status_code == 200
    monkeypatch.setattr(media_relay.time, "time", lambda: 1800000001)
    assert client.get(url).status_code == 403
    assert client.head(url).status_code == 403


def test_signature_expiry_and_duplicate_query_tampering_are_denied(client, relay_asset):
    url = share(client, relay_asset, seconds=100)["url"]
    query = parse_qs(urlsplit(url).query)
    signature = query["signature"][0]
    bad_signature = ("1" if signature[0] != "1" else "2") + signature[1:]
    assert client.get(altered_url(url, signature=bad_signature)).status_code == 403
    assert client.get(altered_url(url, expires=int(query["expires"][0]) + 1)).status_code == 403
    assert client.get(url + "&expires=" + query["expires"][0]).status_code == 403
    assert client.get(url + "&token=forbidden-device-token").status_code == 403
    assert client.get(urlsplit(url).path).status_code == 403


def test_attachment_deletion_and_missing_file_revoke_link(client, repo, settings, relay_asset):
    url = share(client, relay_asset)["url"]
    path = settings.files_dir / relay_asset["attachment"]["stored_relpath"]
    path.unlink()
    assert client.get(url).status_code == 404
    assert client.post(relay_asset["share_path"], headers=relay_asset["headers"], json={}).status_code == 404
    path.write_bytes(relay_asset["content"])
    repo.connection.execute("DELETE FROM attachments WHERE id = ?", (relay_asset["attachment"]["id"],))
    repo.connection.commit()
    assert client.get(url).status_code == 404


def test_replacing_attachment_metadata_revokes_existing_link(client, repo, relay_asset):
    url = share(client, relay_asset)["url"]
    repo.connection.execute("UPDATE attachments SET sha256 = ? WHERE id = ?",
                            ("changed-digest", relay_asset["attachment"]["id"]))
    repo.connection.commit()
    assert client.get(url).status_code == 403


def test_links_survive_app_restart_with_persistent_secret(client, settings, relay_asset):
    url = share(client, relay_asset)["url"]
    restarted = TestClient(create_app(settings))
    assert restarted.get(url).content == relay_asset["content"]
    rotated = TestClient(create_app(replace(settings, secret_key="rotated-test-server-secret")))
    assert rotated.get(url).status_code == 403


def test_forwarded_hub_prefix_and_https_are_in_public_url(client, relay_asset):
    headers = {**relay_asset["headers"], "X-Forwarded-Prefix": "/hub", "X-Forwarded-Proto": "https",
               "Host": "hub.example.test", "X-Forwarded-Host": "ignored.example.test"}
    url = share(client, relay_asset, headers=headers)["url"]
    parts = urlsplit(url)
    assert parts.scheme == "https"
    assert parts.netloc == "hub.example.test"
    assert parts.path == f"/hub/media/{relay_asset['attachment']['id']}"


def test_capability_check_is_authenticated_and_does_not_upload(client, settings, relay_asset, repo):
    initial = len(repo.list_attachments(relay_asset["project"]["id"]))
    assert client.get("/api/media-share/capabilities").status_code == 401
    response = client.get("/api/media-share/capabilities", headers=relay_asset["headers"])
    assert response.json() == {"supported": True, "max_expires_in_seconds": 86400,
                               "max_upload_bytes": settings.max_upload_bytes}
    assert len(repo.list_attachments(relay_asset["project"]["id"])) == initial


def test_default_server_secret_cannot_mint_or_accept_public_links(client, app, relay_asset):
    app.state.settings.secret_key = "local-dev-change-me"
    assert client.post(relay_asset["share_path"], headers=relay_asset["headers"], json={}).status_code == 503
    assert client.get("/api/media-share/capabilities", headers=relay_asset["headers"]).status_code == 503


@pytest.mark.parametrize("kind", ["traversal", "directory"])
def test_attachment_path_never_escapes_file_store_or_lists_directories(client, repo, settings, relay_asset, kind):
    outside = settings.data_dir / "outside-secret.txt"
    outside.write_text("private data", encoding="utf-8")
    stored_path = "../outside-secret.txt" if kind == "traversal" else "."
    repo.connection.execute("UPDATE attachments SET stored_relpath = ? WHERE id = ?",
                            (stored_path, relay_asset["attachment"]["id"]))
    repo.connection.commit()
    assert client.post(relay_asset["share_path"], headers=relay_asset["headers"], json={}).status_code == 404
    assert client.get("/media/").status_code == 404


def test_unsupported_options_are_not_silently_accepted(client, relay_asset):
    response = client.post(relay_asset["share_path"], headers=relay_asset["headers"],
                           json={"expires_in_seconds": 10, "device_token": "do-not-embed"})
    assert response.status_code == 422


@pytest.mark.parametrize("filename,content_type", [
    ("active.html", "text/html"), ("active.svg", "image/svg+xml"),
    ("pretend.html", "video/mp4"), ("pretend.svg", "image/png"),
    ("pretend.png", "text/html"), ("pretend.mp4", "image/svg+xml"),
])
def test_active_documents_cannot_receive_or_use_media_links(client, repo, relay_asset, filename, content_type):
    url = share(client, relay_asset)["url"]
    repo.connection.execute("UPDATE attachments SET original_filename = ?, content_type = ? WHERE id = ?",
                            (filename, content_type, relay_asset["attachment"]["id"]))
    repo.connection.commit()
    response = client.post(relay_asset["share_path"], headers=relay_asset["headers"], json={})
    assert response.status_code == 415
    assert client.get(url).status_code == 415
    assert client.head(url).status_code == 415
    # This restriction belongs only to the new public-media feature.
    assert client.get(relay_asset["private_path"], headers=relay_asset["headers"]).status_code == 200


@pytest.mark.parametrize("filename,content_type,expected", [
    ("original.MOV", "application/octet-stream", "video/quicktime"),
    ("scene.webm", "video/webm", "video/webm"),
    ("reference.png", "image/png", "image/png"),
    ("reference.jpeg", "image/jpeg", "image/jpeg"),
])
def test_supported_media_has_correct_public_content_type(client, repo, relay_asset, filename, content_type, expected):
    repo.connection.execute("UPDATE attachments SET original_filename = ?, content_type = ? WHERE id = ?",
                            (filename, content_type, relay_asset["attachment"]["id"]))
    repo.connection.commit()
    url = share(client, relay_asset)["url"]
    response = client.head(url)
    assert response.status_code == 200
    assert response.headers["content-type"] == expected
