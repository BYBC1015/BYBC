from pathlib import Path

from codex_hub.config import Settings


def test_settings_keep_codex_hub_paths_separate(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "state")

    assert settings.database_path == tmp_path / "state" / "codex_hub.db"
    assert settings.files_dir == tmp_path / "state" / "files"
    assert "/srv/sw-media/uploads" not in str(settings.files_dir).replace("\\", "/")
    assert settings.bind_host == "127.0.0.1"
    assert settings.bind_port == 8797
