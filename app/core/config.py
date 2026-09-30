from datetime import timedelta
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Prefixes of Stripe keys that move real money.
STRIPE_LIVE_KEY_PREFIXES = ("sk_live_", "rk_live_")

# Route name -> models to try, in order: "<provider>/<model>" or "mock". Customers ask for the
# route; Tollgate picks the provider. Targets whose provider has no API key are skipped.
DEFAULT_ROUTES: dict[str, list[str]] = {
    "fast-chat": ["groq/llama-3.1-8b-instant", "gemini/gemini-2.5-flash"],
}

# How long a budget reservation may stay open before the sweep releases it as leaked.
RESERVATION_TTL = timedelta(minutes=10)

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

    # max_tokens for requests that send neither max_tokens nor max_completion_tokens. Every
    # request needs a cap: it bounds the worst-case cost that budgets reserve.
    default_max_tokens: int = Field(default=1024, gt=0)

    # Provider timeouts, in seconds: connecting, the first token of a streamed answer, and the
    # whole answer. A provider that connects and then goes silent can't hang a request forever.
    # (A non-streaming answer arrives whole, so only connect and total apply to it.)
    provider_connect_timeout_s: float = Field(default=5.0, gt=0)
    provider_first_token_timeout_s: float = Field(default=30.0, gt=0)
    provider_total_timeout_s: float = Field(default=120.0, gt=0)

    # Circuit breaker per provider: after this many failures within the window, stop sending
    # it requests for breaker_open_s, then let one test request through.
    breaker_failure_threshold: int = Field(default=5, gt=0)
    breaker_window_s: float = Field(default=30.0, gt=0)
    breaker_open_s: float = Field(default=30.0, gt=0)

    # Routes (see DEFAULT_ROUTES). From the environment as JSON:
    # ROUTES='{"fast-chat": ["groq/llama-3.1-8b-instant", "gemini/gemini-2.5-flash"]}'
    routes: dict[str, list[str]] = Field(default_factory=lambda: dict(DEFAULT_ROUTES))

    # The mock provider (model "mock"): fake answers that cost nothing, for tests and demos.
    mock_delay_ms: int = Field(default=0, ge=0)
    mock_output_tokens: int = Field(default=32, gt=0)
    mock_error_rate: float = Field(default=0.0, ge=0.0, le=1.0)

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

    @field_validator("routes")
    @classmethod
    def _check_routes(cls, routes: dict[str, list[str]]) -> dict[str, list[str]]:
        for name, targets in routes.items():
            # A route name must not look like a model, or it would hide one.
            if not name or "/" in name or name == "mock":
                raise ValueError(f"route name {name!r} must not be empty, 'mock' or contain '/'")
            if not targets:
                raise ValueError(f"route {name!r} needs at least one model")
            for target in targets:
                # Models only, not other routes: routes never chain or loop.
                if target != "mock" and "/" not in target:
                    raise ValueError(
                        f"route {name!r}: {target!r} must be 'mock' or '<provider>/<model>'"
                    )
        return routes

    @model_validator(mode="after")
    def _routes_fit_in_a_reservation(self) -> "Settings":
        # Each model in a route may use the full total timeout. If trying them all could
        # outlast the reservation, the sweep would release money still in use.
        limit = RESERVATION_TTL.total_seconds()
        for name, targets in self.routes.items():
            if len(targets) * self.provider_total_timeout_s >= limit:
                raise ValueError(
                    f"route {name!r}: {len(targets)} models x {self.provider_total_timeout_s:g} s "
                    f"could outlast a {limit:g} s budget reservation"
                )
        return self

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
