from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import app


def test_settings_read_from_environment(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db:5432/x")
    monkeypatch.setenv("REDIS_URL", "redis://cache:6379/1")

    settings = Settings(_env_file=None)

    assert settings.environment == "test"
    assert settings.debug is True
    assert settings.database_url == "postgresql+psycopg://u:p@db:5432/x"
    assert settings.redis_url == "redis://cache:6379/1"


def test_settings_default_to_the_compose_services(monkeypatch):
    for name in ("ENVIRONMENT", "DEBUG", "DATABASE_URL", "REDIS_URL"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)

    assert settings.environment == "local"
    assert settings.debug is False
    assert settings.database_url.endswith("@localhost:5434/tollgate")
    assert settings.redis_url == "redis://localhost:6381/0"


def test_app_serves_docs():
    response = TestClient(app).get("/docs")

    assert response.status_code == 200
