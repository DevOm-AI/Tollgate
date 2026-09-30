import hashlib
import re

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.security import generate_api_key, hash_api_key


def test_generated_key_looks_like_tg_live():
    new_key = generate_api_key()

    assert re.fullmatch(r"tg_live_[A-Za-z0-9]{32}", new_key.key)


def test_generated_key_stores_its_hash_and_prefix():
    new_key = generate_api_key()

    assert new_key.key_hash == hashlib.sha256(new_key.key.encode()).hexdigest()
    assert new_key.prefix == new_key.key.removeprefix("tg_live_")[:8]


def test_generated_keys_differ():
    keys = {generate_api_key().key for _ in range(100)}

    assert len(keys) == 100


def test_hash_is_stable_and_hex():
    assert hash_api_key("tg_live_abc") == hash_api_key("tg_live_abc")
    assert re.fullmatch(r"[0-9a-f]{64}", hash_api_key("tg_live_abc"))


def test_admin_key_is_optional(monkeypatch):
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)

    assert Settings(_env_file=None).admin_api_key is None


def test_admin_key_must_be_long(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "short-SECRET")

    with pytest.raises(ValidationError, match="at least 32") as exc_info:
        Settings(_env_file=None)

    assert "SECRET" not in str(exc_info.value)


def test_admin_key_cannot_be_a_customer_key(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", generate_api_key().key)

    with pytest.raises(ValidationError, match="must not be a customer key"):
        Settings(_env_file=None)
