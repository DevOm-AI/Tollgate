import json
import socket
import threading
import time
import uuid
from collections.abc import Iterator

import anyio
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
from app.providers.base import ChatCompletionRequest, ProviderError
from app.providers.catalog import build_catalog, get_catalog
from app.providers.tokens import count_prompt_tokens, count_text_tokens
from tests.conftest import FAKE_PRICE, MODEL, STREAM_WORDS, create_key

STREAMED_TEXT = "".join(STREAM_WORDS)
HI = ChatCompletionRequest.model_validate(
    {"model": MODEL, "messages": [{"role": "user", "content": "Hi"}]}
)


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


def test_stream_without_usage_is_billed_by_counting_tokens(api, provider, db_engine):
    created = create_key(api)
    provider.send_usage = False

    stream(api, created["key"])

    prompt = count_prompt_tokens(HI)
    assert spend(db_engine, created["id"]) == (price(prompt, count_text_tokens(STREAMED_TEXT)), 0)
    request = logged(db_engine, created["id"])
    assert (request.input_tokens, request.output_tokens) == (prompt, 4)


def test_counted_output_is_capped_at_max_tokens(api, provider, db_engine):
    # The provider ignores max_tokens=2, streams 4 tokens, and sends no usage.
    created = create_key(api)
    provider.send_usage = False

    stream(api, created["key"], max_tokens=2)

    assert logged(db_engine, created["id"]).output_tokens == 2


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
    expected = price(count_prompt_tokens(HI), count_text_tokens(sent))
    assert spend(db_engine, created["id"]) == (expected, 0)
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


def test_client_disconnect_stops_the_provider_and_bills_what_was_generated(
    api, provider, live_url, db_engine
):
    created = create_key(api)
    provider.chunk_delay_s = 0.5

    with httpx2.stream(
        "POST",
        f"{live_url}/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "Hi"}], "stream": True},
        headers={"Authorization": f"Bearer {created['key']}"},
        timeout=10,
    ) as response:
        for line in response.iter_lines():
            if '"content"' in line:
                break  # Read one word, then hang up.

    deadline = time.monotonic() + 5
    while True:
        with Session(db_engine) as session:
            request = session.scalars(
                select(RequestLog).where(RequestLog.key_id == uuid.UUID(created["id"]))
            ).one_or_none()
        if request is not None or time.monotonic() > deadline:
            break
        time.sleep(0.05)

    assert request is not None, "the stream was never settled"
    assert request.status == "cancelled"
    # The provider was stopped early, not left generating tokens nobody reads.
    assert not provider.stream_completed
    assert provider.streamed_chunks < len(STREAM_WORDS)
    # Billed for what the provider generated before the hang-up, nothing more.
    generated = "".join(STREAM_WORDS[: provider.streamed_chunks])
    assert request.output_tokens == count_text_tokens(generated)
    assert spend(db_engine, created["id"])[1] == 0


def call_asgi_with_hang_up(key: str, *, hang_up_by: str) -> None:
    """POST a stream straight to the app as an ASGI 2.4 server would, and hang up:

    - failed_send: after the first word, the next send fails (OSError)
    - disconnect: after the first word, an http.disconnect arrives
    - failed_start: sending the response start fails, before any body is sent
    - early_disconnect: the client is gone before the first body chunk
    """
    body = json.dumps(
        {"model": MODEL, "messages": [{"role": "user", "content": "Hi"}], "stream": True}
    ).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"test"),
            (b"content-type", b"application/json"),
            (b"authorization", f"Bearer {key}".encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }

    async def main() -> None:
        first_word_sent = anyio.Event()
        request_read = False

        async def receive() -> dict:
            nonlocal request_read
            if not request_read:
                request_read = True
                return {"type": "http.request", "body": body, "more_body": False}
            if hang_up_by == "early_disconnect":
                return {"type": "http.disconnect"}
            if hang_up_by == "disconnect":
                await first_word_sent.wait()
                return {"type": "http.disconnect"}
            await anyio.sleep_forever()

        async def send(message: dict) -> None:
            if hang_up_by == "failed_start" and message["type"] == "http.response.start":
                raise OSError("connection reset by peer")
            if first_word_sent.is_set() and hang_up_by == "failed_send":
                raise OSError("connection reset by peer")
            if message["type"] == "http.response.body" and b'"content"' in message["body"]:
                first_word_sent.set()

        with anyio.fail_after(10):
            await app(scope, receive, send)

    anyio.run(main)


@pytest.mark.parametrize("hang_up_by", ["failed_send", "disconnect"])
def test_hang_up_stops_the_provider_under_asgi_2_4(api, provider, db_engine, hang_up_by):
    created = create_key(api)
    provider.chunk_delay_s = 0.3

    call_asgi_with_hang_up(created["key"], hang_up_by=hang_up_by)

    request = logged(db_engine, created["id"])
    assert request.status == "cancelled"
    assert not provider.stream_completed
    assert provider.streamed_chunks < len(STREAM_WORDS)
    assert spend(db_engine, created["id"])[1] == 0


@pytest.mark.parametrize("hang_up_by", ["failed_start", "early_disconnect"])
def test_hang_up_before_the_first_chunk_is_sent_still_settles(api, provider, db_engine, hang_up_by):
    # failed_start is the case where the forwarding generator never starts, so only the
    # response's fallback can settle it; early_disconnect usually races just past that.
    created = create_key(api)
    provider.chunk_delay_s = 0.3

    call_asgi_with_hang_up(created["key"], hang_up_by=hang_up_by)

    request = logged(db_engine, created["id"])
    assert request.status == "cancelled"
    # The provider had generated its first word, so that's billed; nothing is left held.
    assert request.output_tokens == count_text_tokens(STREAM_WORDS[0])
    assert spend(db_engine, created["id"])[1] == 0
    assert not provider.stream_completed


def test_stream_with_no_chunks_ends_cleanly_and_bills_nothing(api, provider, db_engine):
    created = create_key(api)

    async def empty(request):
        return
        yield  # An async generator that sends nothing.

    provider.stream = empty

    response = stream(api, created["key"])

    assert response.status_code == 200
    assert events(response) == ["[DONE]"]
    assert spend(db_engine, created["id"]) == (0, 0)


def test_unexpected_error_opening_a_stream_releases_the_hold(api, provider, db_engine):
    created = create_key(api)
    provider.error = RuntimeError("bug in an adapter")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "Hi"}], "stream": True},
        headers={"Authorization": f"Bearer {created['key']}"},
    )

    assert response.status_code == 500
    assert spend(db_engine, created["id"]) == (0, 0)
    assert logged(db_engine, created["id"]).status == "error"


def test_streamed_tool_call_arguments_are_counted(api, provider, db_engine):
    created = create_key(api)
    arguments = '{"city": "Pune", "units": "metric"}'

    async def tool_call_stream(request):
        base = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m"}
        call = {"index": 0, "function": {"name": "weather", "arguments": arguments}}
        yield base | {"choices": [{"index": 0, "delta": {"tool_calls": [call]}}]}
        yield base | {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}

    provider.stream = tool_call_stream

    stream(api, created["key"])

    assert logged(db_engine, created["id"]).output_tokens == count_text_tokens(arguments)
