from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App settings, read from environment variables (and .env as a fallback)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Literal["local", "test", "production"] = "local"
    debug: bool = False

    # One URL for the app (async) and Alembic (sync): psycopg 3 drives both.
    # The defaults are the docker compose services as seen from the host.
    database_url: str = "postgresql+psycopg://tollgate:tollgate@localhost:5434/tollgate"
    redis_url: str = "redis://localhost:6381/0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
