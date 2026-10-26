from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codex_hub.config import Settings
from codex_hub.db import connect_database, initialize_schema
from codex_hub.main import create_app
from codex_hub.repository import Repository


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "state",
        secret_key="test-secret-key",
        owner_username="owner",
        owner_password="owner-password",
    )


@pytest.fixture
def db(settings: Settings):
    settings.ensure_directories()
    connection = connect_database(settings.database_path)
    initialize_schema(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def repo(db):
    return Repository(db)


@pytest.fixture
def app(settings: Settings):
    settings.ensure_directories()
    return create_app(settings)


@pytest.fixture
def client(app):
    return TestClient(app)
