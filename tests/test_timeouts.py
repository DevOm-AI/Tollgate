import asyncio
import json
import time
import uuid

import httpx2
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.http import provider_timeout
from app.main import app
from app.models import KeySpend, RequestLog
from app.providers.base import ChatCompletionRequest, ProviderTimeout
from app.providers.groq import GroqProvider
from app.providers.tokens import count_text_tokens
from tests.conftest import ADMIN_KEY, STREAM_WORDS, chat, create_key


def use_timeouts(**timeouts: float) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, admin_api_key=ADMIN_KEY, **timeouts
    )


def spend(db_engine: Engine, key_id: str) -> tuple[int, int]:
    with Session(db_engine) as session:
        row = session.scalars(select(KeySpend).where(KeySpend.key_id == uuid.UUID(key_id))).one()
        return row.spent_micros, row.reserved_micros


def logged(db_engine: Engine, key_id: str) -> RequestLog:
    with Session(db_engine) as session:
        return session.scalars(
            select(RequestLog).where(RequestLog.key_id == uuid.UUID(key_id))
        ).one()


def timed(send) -> tuple[httpx2.Response, float]:
    started = time.monotonic()
    response = send()
    return response, time.monotonic() - started


# --- Settings ---


def test_timeouts_default_to_5_30_and_120_seconds():
    settings = Settings(_env_file=None)

    assert (
        settings.provider_connect_timeout_s,
        settings.provider_first_token_timeout_s,
        settings.provider_total_timeout_s,
    ) == (5.0, 30.0, 120.0)


def test_http_client_connects_within_the_connect_timeout():
    timeout = provider_timeout(Settings(_env_file=None, provider_connect_timeout_s=2))

    assert timeout.connect == 2
    assert timeout.read == 120


@pytest.mark.parametrize("name", ["connect", "first_token", "total"])
def test_timeouts_must_be_positive(name: str):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{f"provider_{name}_timeout_s": 0})


# --- Adapters ---


@pytest.mark.parametrize("error", [httpx2.ConnectTimeout, httpx2.ReadTimeout, httpx2.PoolTimeout])
def test_http_timeouts_are_provider_timeouts(error):
    def time_out(req: httpx2.Request) -> httpx2.Response:
        raise error("timed out", request=req)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(time_out))
    request = ChatCompletionRequest.model_validate(
        {"model": "m", "messages": [{"role": "user", "content": "Hi"}]}
    )

    with pytest.raises(ProviderTimeout):
        asyncio.run(GroqProvider("k", client).complete(request))


# --- Non-streaming ---


def test_answer_slower_than_the_total_timeout_is_a_504(api, provider, db_engine):
    use_timeouts(provider_total_timeout_s=0.3)
    created = create_key(api)
    provider.delay_s = 5

    response, elapsed = timed(lambda: chat(api, created["key"]))

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "provider_timeout"
    assert elapsed < 2
    assert spend(db_engine, created["id"]) == (0, 0)
    assert logged(db_engine, created["id"]).status == "timeout"


def test_answer_within_the_timeout_is_unaffected(api, provider):
    use_timeouts(provider_total_timeout_s=2)
    key = create_key(api)["key"]
    provider.delay_s = 0.1

    assert chat(api, key).status_code == 200


# --- Streaming ---


def stream(api, key: str) -> httpx2.Response:
    return api.post(
        "/v1/chat/completions",
        json={
            "model": "fake/test-model",
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
        },
        headers={"Authorization": f"Bearer {key}"},
    )


def test_stream_without_a_first_token_in_time_is_a_504(api, provider, db_engine):
    use_timeouts(provider_first_token_timeout_s=0.3)
    created = create_key(api)
    provider.delay_s = 5

    response, elapsed = timed(lambda: stream(api, created["key"]))

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "provider_timeout"
    assert elapsed < 2
    assert spend(db_engine, created["id"]) == (0, 0)
    assert logged(db_engine, created["id"]).status == "timeout"


def test_first_token_timeout_never_exceeds_the_total(api, provider):
    use_timeouts(provider_first_token_timeout_s=30, provider_total_timeout_s=0.3)
    key = create_key(api)["key"]
    provider.delay_s = 5

    response, elapsed = timed(lambda: stream(api, key))

    assert response.status_code == 504
    assert elapsed < 2


def test_stream_going_silent_ends_at_the_total_timeout(api, provider, db_engine):
    # The first word comes at once, then the provider goes quiet for longer than allowed.
    use_timeouts(provider_total_timeout_s=0.8)
    created = create_key(api)
    provider.chunk_delay_s = 5

    response, elapsed = timed(lambda: stream(api, created["key"]))

    assert response.status_code == 200
    events = [line.removeprefix("data: ") for line in response.text.split("\n\n") if line]
    assert json.loads(events[-1])["error"]["code"] == "provider_timeout"
    assert elapsed < 3
    request = logged(db_engine, created["id"])
    assert request.status == "timeout"
    # Billed for the word that did arrive, and nothing is left held.
    assert request.output_tokens == count_text_tokens(STREAM_WORDS[0])
    assert spend(db_engine, created["id"])[1] == 0


def test_stream_within_the_timeouts_is_unaffected(api, provider):
    use_timeouts(provider_first_token_timeout_s=1, provider_total_timeout_s=3)
    key = create_key(api)["key"]
    provider.chunk_delay_s = 0.1

    response = stream(api, key)

    assert response.status_code == 200
    assert response.text.endswith("data: [DONE]\n\n")


def test_role_only_first_chunk_does_not_count_as_the_first_token(api, provider, db_engine):
    use_timeouts(provider_first_token_timeout_s=0.3)
    created = create_key(api)
    provider.role_chunk_then_pause_s = 5

    response, elapsed = timed(lambda: stream(api, created["key"]))

    # Nothing but metadata before the deadline: still a clean 504, and nothing billed.
    assert response.status_code == 504
    assert elapsed < 2
    assert spend(db_engine, created["id"]) == (0, 0)


def test_role_only_first_chunk_is_still_forwarded(api, provider):
    use_timeouts(provider_first_token_timeout_s=2)
    key = create_key(api)["key"]
    provider.role_chunk_then_pause_s = 0.1

    response = stream(api, key)

    events = [line.removeprefix("data: ") for line in response.text.split("\n\n") if line]
    first = json.loads(events[0])["choices"][0]["delta"]
    assert first == {"role": "assistant", "content": ""}
    assert events[-1] == "[DONE]"
