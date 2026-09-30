import json
import socket
import threading
import time
import uuid
from collections.abc import Iterator

import httpx2
import openai
import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.billing.pricing import cost_micros
from app.core.config import Settings
from app.core.http import http_client
from app.main import app
from app.models import KeySpend, RequestLog, UsageOutbox
from app.providers.base import ProviderError
from app.providers.catalog import build_catalog, get_catalog
from tests.conftest import FAKE_PRICE, MODEL, STREAM_WORDS, create_key

STREAMED_TEXT = "".join(STREAM_WORDS)


def price(input_tokens: int, output_tokens: int) -> int:
    return cost_micros(
        input_tokens,
        output_tokens,
        FAKE_PRICE["input_micros_per_1k"],
        FAKE_PRICE["output_micros_per_1k"],
    )


def stream(api: TestClient, key: str, **body) -> httpx2.Response:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": True,
    } | body
    return api.post(
        "/v1/chat/completions", json=payload, headers={"Authorization": f"Bearer {key}"}
    )


def events(response: httpx2.Response) -> list[str]:
    return [line.removeprefix("data: ") for line in response.text.split("\n\n") if line]


def openai_client(key: str) -> openai.OpenAI:
    return openai.OpenAI(
        base_url="http://testserver/v1", api_key=key, http_client=TestClient(app), max_retries=0
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


# --- Passing the stream through ---


def test_openai_library_streams_through_tollgate(api, provider):
    key = create_key(api)["key"]

    chunks = list(
        openai_client(key).chat.completions.create(
            model=MODEL, messages=[{"role": "user", "content": "Hi"}], stream=True
        )
    )

    text = "".join(chunk.choices[0].delta.content or "" for chunk in chunks if chunk.choices)
    assert text == STREAMED_TEXT
    assert chunks[-1].choices[0].finish_reason == "stop"
    # The client didn't ask for usage, so the chunk Tollgate asked for isn't passed on.
    assert all(chunk.usage is None for chunk in chunks)


def test_client_that_asks_for_usage_gets_it(api, provider):
    key = create_key(api)["key"]

    chunks = list(
        openai_client(key).chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "Hi"}],
            stream=True,
            stream_options={"include_usage": True},
        )
    )

    assert chunks[-1].choices == []
    assert chunks[-1].usage.completion_tokens == 4


def test_provider_is_asked_to_stream_with_usage(api, provider):
    key = create_key(api)["key"]

    stream(api, key, stream_options={"include_usage": False})

    sent = provider.requests[0]
    assert sent.stream is True
    assert sent.model_extra["stream_options"] == {"include_usage": True}


def test_stream_is_server_sent_events_ending_with_done(api, provider):
    key = create_key(api)["key"]

    response = stream(api, key)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["X-Accel-Buffering"] == "no"
    assert "X-RateLimit-Remaining-Requests" in response.headers
    data = events(response)
    assert data[-1] == "[DONE]"
    assert [json.loads(event)["object"] for event in data[:-1]] == ["chat.completion.chunk"] * 5


def test_mock_model_streams_through_the_openai_library(api):
    app.dependency_overrides[get_catalog] = lambda: build_catalog(
        Settings(_env_file=None, mock_output_tokens=6), http_client
    )
    key = create_key(api)["key"]

    chunks = list(
        openai_client(key).chat.completions.create(
            model="mock", messages=[{"role": "user", "content": "Hi"}], stream=True
        )
    )

    words = [c.choices[0].delta.content for c in chunks if c.choices and c.choices[0].delta.content]
    assert len(words) == 6


# --- Billing a stream ---


def test_stream_is_billed_from_the_providers_usage(api, provider, db_engine):
    created = create_key(api)

    stream(api, created["key"])

    assert spend(db_engine, created["id"]) == (price(3, 4), 0)
    request = logged(db_engine, created["id"])
    assert (request.status, request.input_tokens, request.output_tokens) == ("ok", 3, 4)
    with Session(db_engine) as session:
        usage = session.scalars(
            select(UsageOutbox).where(UsageOutbox.customer_id == uuid.UUID(created["customer_id"]))
        ).one()
    assert usage.cost_micros == price(3, 4)


def test_stream_without_usage_is_billed_from_an_estimate(api, provider, db_engine):
    created = create_key(api)
    provider.send_usage = False

    stream(api, created["key"])

    # About 4 characters per token: "Hi" is 1 prompt token, the answer 6 output tokens.
    output = -(-len(STREAMED_TEXT) // 4)
    assert spend(db_engine, created["id"]) == (price(1, output), 0)


def test_provider_failing_before_any_output_is_a_502_and_bills_nothing(api, provider, db_engine):
    created = create_key(api)
    provider.error = ProviderError("fake: HTTP 503", status_code=503)

    response = stream(api, created["key"])

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_error"
    assert spend(db_engine, created["id"]) == (0, 0)
    assert logged(db_engine, created["id"]).status == "provider_error"


def test_provider_rejecting_a_stream_is_a_400(api, provider):
    key = create_key(api)["key"]
    provider.error = ProviderError("fake: HTTP 400", status_code=400, upstream_message="bad")

    assert stream(api, key).status_code == 400


def test_stream_cut_midway_sends_an_error_event_and_bills_what_was_sent(api, provider, db_engine):
    created = create_key(api)
    provider.break_before_chunk = 2

    response = stream(api, created["key"])

    assert response.status_code == 200
    data = events(response)
    assert json.loads(data[-1])["error"]["code"] == "provider_error"
    assert "[DONE]" not in data
    sent = "".join(STREAM_WORDS[:2])
    assert spend(db_engine, created["id"]) == (price(1, -(-len(sent) // 4)), 0)
    assert logged(db_engine, created["id"]).status == "provider_error"


def test_openai_library_raises_on_a_stream_cut_midway(api, provider):
    key = create_key(api)["key"]
    provider.break_before_chunk = 2

    with pytest.raises(openai.APIError):
        list(
            openai_client(key).chat.completions.create(
                model=MODEL, messages=[{"role": "user", "content": "Hi"}], stream=True
            )
        )


def test_streams_count_against_the_budget(api, provider):
    created = create_key(api, monthly_budget_micros=0)

    response = stream(api, created["key"])

    assert response.status_code == 402


# --- Arriving word by word, on a real server ---


@pytest.fixture
def live_url(api) -> Iterator[str]:
    """The app on a real HTTP server, so responses aren't collected before they're read."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "server didn't start"
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_stream_arrives_word_by_word(api, provider, live_url):
    key = create_key(api)["key"]
    provider.chunk_delay_s = 0.3

    arrivals = []
    started = time.monotonic()
    with httpx2.stream(
        "POST",
        f"{live_url}/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "Hi"}], "stream": True},
        headers={"Authorization": f"Bearer {key}"},
        timeout=10,
    ) as response:
        for line in response.iter_lines():
            if line.startswith("data: ") and '"content"' in line:
                arrivals.append(time.monotonic() - started)

    assert len(arrivals) == len(STREAM_WORDS)
    # The first word shows up long before the answer is done: nothing is buffered.
    assert arrivals[0] < 0.25
    assert arrivals[-1] - arrivals[0] >= 0.8
