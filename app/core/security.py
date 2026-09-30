import hashlib
import hmac
import secrets
import string
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import CUSTOMER_KEY_PREFIX, Settings, get_settings

# 32 characters from 62 gives about 190 bits: too many to guess, so plain SHA-256 is enough.
KEY_ALPHABET = string.ascii_letters + string.digits
KEY_SECRET_LENGTH = 32
# How much of the secret part is stored in the clear, so the dashboard can tell keys apart.
KEY_PREFIX_LENGTH = 8


@dataclass(frozen=True)
class NewApiKey:
    """A freshly made key. `key` is shown once; only `key_hash` and `prefix` are stored."""

    key: str
    key_hash: str
    prefix: str


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_api_key() -> NewApiKey:
    secret = "".join(secrets.choice(KEY_ALPHABET) for _ in range(KEY_SECRET_LENGTH))
    key = CUSTOMER_KEY_PREFIX + secret
    return NewApiKey(key=key, key_hash=hash_api_key(key), prefix=secret[:KEY_PREFIX_LENGTH])


# auto_error=False: a missing header gets the same 401 as a wrong key, not FastAPI's 403.
bearer = HTTPBearer(auto_error=False)


def require_admin(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Let the request through only with the admin key. Customer keys never pass."""
    if settings.admin_api_key is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin API is disabled: ADMIN_API_KEY is not set",
        )
    expected = settings.admin_api_key.get_secret_value().encode()
    presented = credentials.credentials.encode() if credentials else b""
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin key",
            headers={"WWW-Authenticate": "Bearer"},
        )
