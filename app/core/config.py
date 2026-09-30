from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Prefixes of Stripe keys that move real money.
STRIPE_LIVE_KEY_PREFIXES = ("sk_live_", "rk_live_")

# Customer keys start with this; the admin key must not look like one.
CUSTOMER_KEY_PREFIX = "tg_live_"
ADMIN_API_KEY_MIN_LENGTH = 32


def normalize_database_url(url: str) -> str:
    """Point plain Postgres URLs (what Neon's console copies) at the psycopg 3 driver."""
    for scheme in ("postgres://", "postgresql://"):
        if url.startswith(scheme):
            return "postgresql+psycopg://" + url.removeprefix(scheme)
    return url


class Settings(BaseSettings):
    """App settings, read from environment variables (and .env as a fallback)."""

    # env_ignore_empty: a key left blank in .env (KEY=) counts as unset, not as "".
    # hide_input_in_errors: a rejected setting's value (maybe a secret) stays out of errors.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
        hide_input_in_errors=True,
    )

    environment: Literal["local", "test", "production"] = "local"
    debug: bool = False

    # One URL for the app (async) and Alembic (sync): psycopg 3 drives both.
    # The defaults are the docker compose services as seen from the host.
    database_url: str = "postgresql+psycopg://tollgate:tollgate@localhost:5434/tollgate"
    redis_url: str = "redis://localhost:6381/0"

    # LLM provider keys. Unset = that provider isn't available.
    gemini_api_key: SecretStr | None = None
    groq_api_key: SecretStr | None = None

    # Stripe secret key (sk_test_...). Unset = usage isn't reported to Stripe.
    stripe_secret_key: SecretStr | None = None

    # Guards the /admin routes. Unset = those routes are disabled.
    admin_api_key: SecretStr | None = None

    @field_validator("database_url")
    @classmethod
    def _use_psycopg(cls, value: str) -> str:
        return normalize_database_url(value)

    @field_validator("admin_api_key")
    @classmethod
    def _check_admin_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        secret = value.get_secret_value()
        if len(secret) < ADMIN_API_KEY_MIN_LENGTH:
            raise ValueError(
                f"ADMIN_API_KEY must be at least {ADMIN_API_KEY_MIN_LENGTH} characters"
            )
        if secret.startswith(CUSTOMER_KEY_PREFIX):
            raise ValueError("ADMIN_API_KEY must not be a customer key")
        return value

    @model_validator(mode="after")
    def _refuse_live_stripe_key(self) -> "Settings":
        # Refuse to start rather than bill real cards from a laptop or CI.
        key = self.stripe_secret_key
        if (
            self.environment != "production"
            and key is not None
            and key.get_secret_value().startswith(STRIPE_LIVE_KEY_PREFIXES)
        ):
            raise ValueError(
                f"STRIPE_SECRET_KEY is a live key; ENVIRONMENT={self.environment} needs a "
                "test mode key (sk_test_...)"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
