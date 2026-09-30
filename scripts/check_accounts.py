"""Check that every account key in .env works, without printing any of them.

    uv run python -m scripts.check_accounts

Each check is a free, read-only call (a SELECT, a PING, a model list, a balance read).
Exits 1 when a key is missing or doesn't work. Where to get each key: docs/accounts.md.
"""

import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from redis import Redis
from sqlalchemy import create_engine, pool, text

from app.core.config import STRIPE_LIVE_KEY_PREFIXES, normalize_database_url

TIMEOUT_SECONDS = 10

GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/openai/models"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"
STRIPE_BALANCE_URL = "https://api.stripe.com/v1/balance"
HF_WHOAMI_URL = "https://huggingface.co/api/whoami-v2"


class AccountKeys(BaseSettings):
    """Every account key from .env. The hosted URLs are only used here and when deploying."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
        hide_input_in_errors=True,
    )

    neon_database_url: SecretStr | None = None
    upstash_redis_url: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    groq_api_key: SecretStr | None = None
    stripe_secret_key: SecretStr | None = None
    hf_token: SecretStr | None = None


class CheckFailed(Exception):
    """A check reached the service and got a wrong answer. The message is safe to print."""


def check_neon(url: str) -> str:
    engine = create_engine(
        normalize_database_url(url),
        poolclass=pool.NullPool,
        connect_args={"connect_timeout": TIMEOUT_SECONDS},
    )
    try:
        with engine.connect() as connection:
            version = connection.scalar(text("SHOW server_version"))
    finally:
        engine.dispose()
    return f"Postgres {version}"


def check_upstash(url: str) -> str:
    # Upstash only accepts TLS. Its console shows `redis-cli --tls -u redis://...`,
    # and that redis:// URL fails without the flag.
    if not url.startswith("rediss://"):
        raise CheckFailed("use the TLS URL: rediss://default:<password>@<host>:6379")
    client = Redis.from_url(
        url, socket_connect_timeout=TIMEOUT_SECONDS, socket_timeout=TIMEOUT_SECONDS
    )
    try:
        client.ping()
    finally:
        client.close()
    return "PING ok"


def check_gemini(key: str) -> str:
    return f"{len(_get_json(GEMINI_MODELS_URL, key)['data'])} models"


def check_groq(key: str) -> str:
    return f"{len(_get_json(GROQ_MODELS_URL, key)['data'])} models"


def check_stripe(key: str) -> str:
    if key.startswith(STRIPE_LIVE_KEY_PREFIXES):
        raise CheckFailed("this is a live key; use the test mode secret key (sk_test_...)")
    if _get_json(STRIPE_BALANCE_URL, key)["livemode"]:
        raise CheckFailed("Stripe answered in live mode; use the test mode secret key")
    return "test mode"


def check_hf(token: str) -> str:
    whoami = _get_json(HF_WHOAMI_URL, token)
    role = whoami.get("auth", {}).get("accessToken", {}).get("role", "unknown")
    if role == "read":
        # Deploying pushes to the Space's git remote, which needs write access.
        raise CheckFailed(f"{whoami['name']}'s token is read-only; create a write token")
    return f"{whoami['name']} ({role} token)"


def _get_json(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            # Some APIs sit behind Cloudflare, which blocks urllib's default user agent.
            "User-Agent": "tollgate-check-accounts",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Status only: error bodies can quote the key back.
        # Gemini answers a bad key with 400, the others with 401 or 403.
        hint = " (wrong key?)" if exc.code in (400, 401, 403) else ""
        raise CheckFailed(f"HTTP {exc.code} from {urlsplit(url).hostname}{hint}") from None


@dataclass(frozen=True)
class Check:
    label: str
    env_var: str
    probe: Callable[[str], str]


CHECKS = [
    Check("Neon Postgres", "NEON_DATABASE_URL", check_neon),
    Check("Upstash Redis", "UPSTASH_REDIS_URL", check_upstash),
    Check("Gemini", "GEMINI_API_KEY", check_gemini),
    Check("Groq", "GROQ_API_KEY", check_groq),
    Check("Stripe", "STRIPE_SECRET_KEY", check_stripe),
    Check("Hugging Face", "HF_TOKEN", check_hf),
]


@dataclass(frozen=True)
class Result:
    label: str
    status: Literal["ok", "missing", "failed"]
    detail: str


def run_check(check: Check, keys: AccountKeys) -> Result:
    secret: SecretStr | None = getattr(keys, check.env_var.lower())
    if secret is None:
        return Result(check.label, "missing", f"set {check.env_var} in .env")
    value = secret.get_secret_value()
    try:
        detail = check.probe(value)
    except CheckFailed as exc:
        return Result(check.label, "failed", _redact(str(exc), value))
    except Exception as exc:
        # Driver errors can name hosts and users; the value itself is scrubbed below.
        message = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
        return Result(check.label, "failed", _redact(f"{type(exc).__name__}: {message}", value))
    return Result(check.label, "ok", detail)


def _redact(message: str, value: str) -> str:
    """Remove the key, and a URL's password, from a message before printing it."""
    secrets = [value]
    try:
        password = urlsplit(value).password
    except ValueError:
        password = None
    if password:
        secrets.append(password)
    for secret in secrets:
        message = message.replace(secret, "***")
    return message


def main() -> int:
    keys = AccountKeys()
    results = [run_check(check, keys) for check in CHECKS]
    width = max(len(result.label) for result in results)
    for result in results:
        print(f"{result.label:<{width}}  {result.status:<7}  {result.detail}")
    if all(result.status == "ok" for result in results):
        print("\nAll accounts work.")
        return 0
    print("\nSome accounts are missing or failing. See docs/accounts.md.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
