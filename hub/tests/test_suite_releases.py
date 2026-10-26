from codex_hub.releases import list_latest_releases


def test_latest_suite_replaces_legacy_bridge_at_same_download_entry(tmp_path):
    (tmp_path / "CodexBridge-v1.0.23.exe").write_bytes(b"old")
    (tmp_path / "CodexBridge-v1.0.24.exe").write_bytes(b"portable")
    (tmp_path / "CodexHub-Setup-v1.0.24.exe").write_bytes(b"suite")
    (tmp_path / "CodexHub-v0.1.24-debug.apk").write_bytes(b"android")
    (tmp_path / "CodexHub-Setup-v1.0.99.exe.partial").write_bytes(b"incomplete")
    releases = {r.platform: r for r in list_latest_releases(tmp_path)}
    assert releases["windows"].path.name == "CodexHub-Setup-v1.0.24.exe"
    assert releases["windows"].byte_size == 5
    assert releases["android"].version == "0.1.24"


def test_suite_download_endpoint_and_page(client, settings):
    (settings.releases_dir / "CodexBridge-v1.0.22.exe").write_bytes(b"old")
    (settings.releases_dir / "CodexHub-Setup-v1.0.24.exe").write_bytes(b"installer")
    manifest = client.get("/api/releases").json()
    release = next(r for r in manifest["releases"] if r["platform"] == "windows")
    assert release["filename"] == "CodexHub-Setup-v1.0.24.exe"
    assert client.get("/downloads/windows").content == b"installer"
    client.post("/login", data={"username": "owner", "password": "owner-password"})
    page = client.get("/downloads")
    assert "可灵批处理" in page.text and "一次安装" in page.text
