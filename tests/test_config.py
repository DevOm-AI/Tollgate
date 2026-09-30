import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

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


ACCOUNT_KEY_VARS = ("GEMINI_API_KEY", "GROQ_API_KEY", "STRIPE_SECRET_KEY")


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@ep-x.neon.tech/neondb?sslmode=require",
        "postgres://u:p@ep-x.neon.tech/neondb?sslmode=require",
        "postgresql+psycopg://u:p@ep-x.neon.tech/neondb?sslmode=require",
    ],
)
def test_database_url_uses_psycopg(monkeypatch, url: str):
    monkeypatch.setenv("DATABASE_URL", url)

    assert Settings(_env_file=None).database_url == (
        "postgresql+psycopg://u:p@ep-x.neon.tech/neondb?sslmode=require"
    )


def test_account_keys_are_optional(monkeypatch):
    for name in ACCOUNT_KEY_VARS:
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)

    assert settings.gemini_api_key is None
    assert settings.groq_api_key is None
    assert settings.stripe_secret_key is None


def test_blank_account_keys_count_as_unset(monkeypatch):
    # What .env.example ships with: KEY=
    for name in ACCOUNT_KEY_VARS:
        monkeypatch.setenv(name, "")

    settings = Settings(_env_file=None)

    assert settings.gemini_api_key is None
    assert settings.groq_api_key is None
    assert settings.stripe_secret_key is None


def test_account_keys_are_hidden_when_printed(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-VALUE1")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_VALUE2")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_VALUE3")

    settings = Settings(_env_file=None)

    assert settings.gemini_api_key.get_secret_value() == "gemini-VALUE1"
    assert settings.groq_api_key.get_secret_value() == "gsk_VALUE2"
    assert settings.stripe_secret_key.get_secret_value() == "sk_test_VALUE3"
    for printed in (repr(settings), str(settings.model_dump())):
        assert "VALUE" not in printed


@pytest.mark.parametrize("environment", ["local", "test"])
@pytest.mark.parametrize("key", ["sk_live_abc123", "rk_live_abc123"])
def test_live_stripe_key_is_refused_outside_production(monkeypatch, environment: str, key: str):
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("STRIPE_SECRET_KEY", key)

    with pytest.raises(ValidationError, match="live key") as exc_info:
        Settings(_env_file=None)

    assert "abc123" not in str(exc_info.value)


def test_live_stripe_key_is_allowed_in_production(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_abc123")

    assert Settings(_env_file=None).stripe_secret_key.get_secret_value() == "sk_live_abc123"


def test_test_mode_stripe_key_is_accepted(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc123")

    assert Settings(_env_file=None).stripe_secret_key.get_secret_value() == "sk_test_abc123"
