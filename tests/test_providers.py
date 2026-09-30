import asyncio
import json
import random
import time

import httpx2
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.providers.base import ChatCompletionRequest, ProviderError, estimate_prompt_tokens
from app.providers.catalog import Catalog, build_catalog
from app.providers.gemini import GeminiProvider
from app.providers.groq import GroqProvider
from app.providers.mock import MockProvider


def request(**overrides) -> ChatCompletionRequest:
    body = {"model": "m", "messages": [{"role": "user", "content": "Hello there"}]} | overrides
    return ChatCompletionRequest.model_validate(body)


async def collect(stream) -> list[dict]:
    return [chunk async for chunk in stream]


def completion_body(**overrides) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "llama",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hi"}}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    } | overrides


# --- Mock provider ---


def test_mock_answer_has_the_configured_length():
    completion = asyncio.run(MockProvider(output_tokens=7).complete(request()))

    content = completion.choices[0]["message"]["content"]
    assert len(content.split()) == 7
    assert completion.usage.completion_tokens == 7
    assert completion.choices[0]["finish_reason"] == "stop"


def test_mock_answer_is_cut_to_max_tokens():
    completion = asyncio.run(MockProvider(output_tokens=50).complete(request(max_tokens=3)))

    assert completion.usage.completion_tokens == 3
    assert completion.choices[0]["finish_reason"] == "length"


def test_mock_answer_is_cut_to_max_completion_tokens():
    provider = MockProvider(output_tokens=50)

    completion = asyncio.run(provider.complete(request(max_completion_tokens=4)))

    assert completion.usage.completion_tokens == 4


def test_mock_counts_prompt_tokens():
    completion = asyncio.run(MockProvider().complete(request()))

    assert completion.usage.prompt_tokens == estimate_prompt_tokens(request())
    assert completion.usage.total_tokens == (
        completion.usage.prompt_tokens + completion.usage.completion_tokens
    )


def test_prompt_estimate_counts_text_parts():
    parts = [{"type": "text", "text": "a" * 40}, {"type": "image_url", "image_url": {}}]

    assert estimate_prompt_tokens(request(messages=[{"role": "user", "content": parts}])) == 10
    assert estimate_prompt_tokens(request(messages=[{"role": "user", "content": ""}])) == 1


@pytest.mark.parametrize("text", [None, 5, ["a"]])
def test_prompt_estimate_skips_parts_without_string_text(text):
    parts = [{"type": "text", "text": text}, {"type": "text", "text": "a" * 8}]

    assert estimate_prompt_tokens(request(messages=[{"role": "user", "content": parts}])) == 2


def test_mock_waits_for_its_delay():
    started = time.perf_counter()
    asyncio.run(MockProvider(delay_ms=50).complete(request()))

    assert time.perf_counter() - started >= 0.05


@pytest.mark.parametrize(("error_rate", "failures"), [(0.0, 0), (1.0, 20)])
def test_mock_error_rate(error_rate: float, failures: int):
    provider = MockProvider(error_rate=error_rate)
    failed = 0
    for _ in range(20):
        try:
            asyncio.run(provider.complete(request()))
        except ProviderError as exc:
            assert exc.status_code == 503
            failed += 1

    assert failed == failures


def test_mock_error_rate_is_a_share_of_requests():
    provider = MockProvider(error_rate=0.3, rng=random.Random(42))
    failed = 0
    for _ in range(1000):
        try:
            asyncio.run(provider.complete(request()))
        except ProviderError:
            failed += 1

    assert 250 < failed < 350


def test_mock_stream_sends_the_answer_word_by_word():
    chunks = asyncio.run(collect(MockProvider(output_tokens=4).stream(request())))

    words = [chunk["choices"][0]["delta"]["content"] for chunk in chunks[:-1]]
    assert len(words) == 4
    assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert "usage" not in chunks[-1]


def test_mock_stream_ends_with_usage_when_asked():
    stream_request = request(stream_options={"include_usage": True})
    chunks = asyncio.run(collect(MockProvider(output_tokens=4).stream(stream_request)))

    assert chunks[-1]["choices"] == []
    assert chunks[-1]["usage"]["completion_tokens"] == 4


def test_mock_stream_can_fail():
    with pytest.raises(ProviderError):
        asyncio.run(collect(MockProvider(error_rate=1.0).stream(request())))


# --- OpenAI-compatible providers (Groq, Gemini) ---


class Upstream:
    """Stands in for the provider's HTTP API and records what it was sent."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.requests: list[httpx2.Request] = []

    def client(self) -> httpx2.AsyncClient:
        def handle(req: httpx2.Request) -> httpx2.Response:
            self.requests.append(req)
            return self.handler(req)

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle))


@pytest.mark.parametrize(
    ("provider_class", "url"),
    [
        (GroqProvider, "https://api.groq.com/openai/v1/chat/completions"),
        (
            GeminiProvider,
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        ),
    ],
)
def test_provider_posts_to_its_chat_completions_url(provider_class, url: str):
    upstream = Upstream(lambda req: httpx2.Response(200, json=completion_body()))
    provider = provider_class("secret-key", upstream.client())

    completion = asyncio.run(provider.complete(request(max_tokens=16, temperature=0.5)))

    sent = upstream.requests[0]
    assert str(sent.url) == url
    assert sent.headers["Authorization"] == "Bearer secret-key"
    assert json.loads(sent.content) == {
        "model": "m",
        "messages": [{"role": "user", "content": "Hello there"}],
        "max_tokens": 16,
        "stream": False,
        "n": 1,
        "temperature": 0.5,
    }
    assert completion.choices[0]["message"]["content"] == "Hi"


def test_provider_error_status_is_kept_with_its_message():
    upstream = Upstream(
        lambda req: httpx2.Response(404, json={"error": {"message": "model not found"}})
    )
    provider = GroqProvider("k", upstream.client())

    with pytest.raises(ProviderError) as exc_info:
        asyncio.run(provider.complete(request()))

    assert exc_info.value.status_code == 404
    assert exc_info.value.is_client_error
    assert exc_info.value.upstream_message == "model not found"


def test_gemini_list_wrapped_error_message_is_read():
    upstream = Upstream(lambda req: httpx2.Response(400, json=[{"error": {"message": "bad"}}]))

    with pytest.raises(ProviderError) as exc_info:
        asyncio.run(GeminiProvider("k", upstream.client()).complete(request()))

    assert exc_info.value.upstream_message == "bad"


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_provider_server_errors_are_not_client_errors(status_code: int):
    upstream = Upstream(lambda req: httpx2.Response(status_code, text="oops"))

    with pytest.raises(ProviderError) as exc_info:
        asyncio.run(GroqProvider("k", upstream.client()).complete(request()))

    assert exc_info.value.status_code == status_code
    assert not exc_info.value.is_client_error
    assert exc_info.value.upstream_message is None


def test_provider_long_error_message_is_cut():
    upstream = Upstream(lambda req: httpx2.Response(400, json={"error": {"message": "x" * 5000}}))

    with pytest.raises(ProviderError) as exc_info:
        asyncio.run(GroqProvider("k", upstream.client()).complete(request()))

    assert len(exc_info.value.upstream_message) == 500


def test_provider_connection_error_is_a_provider_error():
    def refuse(req: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=req)

    with pytest.raises(ProviderError) as exc_info:
        asyncio.run(GroqProvider("k", Upstream(refuse).client()).complete(request()))

    assert exc_info.value.status_code is None


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(200, text="not json"),
        httpx2.Response(200, json={"id": "x"}),
    ],
)
def test_provider_unreadable_answer_is_a_provider_error(response: httpx2.Response):
    upstream = Upstream(lambda req: response)

    with pytest.raises(ProviderError, match="unreadable"):
        asyncio.run(GroqProvider("k", upstream.client()).complete(request()))


def sse(*events: str) -> httpx2.Response:
    body = "".join(f"data: {event}\n\n" for event in events)
    return httpx2.Response(200, text=body, headers={"Content-Type": "text/event-stream"})


def test_provider_stream_yields_each_chunk_until_done():
    chunk = {"object": "chat.completion.chunk", "choices": [{"delta": {"content": "Hi"}}]}
    upstream = Upstream(
        lambda req: sse(json.dumps(chunk), json.dumps(chunk), "[DONE]", json.dumps(chunk))
    )

    chunks = asyncio.run(collect(GroqProvider("k", upstream.client()).stream(request())))

    assert chunks == [chunk, chunk]
    assert json.loads(upstream.requests[0].content)["stream"] is True


def test_provider_stream_error_status_is_a_provider_error():
    upstream = Upstream(lambda req: httpx2.Response(503, json={"error": {"message": "busy"}}))

    with pytest.raises(ProviderError) as exc_info:
        asyncio.run(collect(GroqProvider("k", upstream.client()).stream(request())))

    assert (exc_info.value.status_code, exc_info.value.upstream_message) == (503, "busy")


def test_provider_stream_bad_chunk_is_a_provider_error():
    upstream = Upstream(lambda req: sse("{not json"))

    with pytest.raises(ProviderError, match="unreadable"):
        asyncio.run(collect(GroqProvider("k", upstream.client()).stream(request())))


# --- Catalog ---


def settings(**overrides) -> Settings:
    keys = {"groq_api_key": None, "gemini_api_key": None}
    return Settings(_env_file=None, **(keys | overrides))


def test_catalog_always_has_the_mock():
    resolved = build_catalog(settings(), httpx2.AsyncClient()).resolve("mock")

    assert resolved.provider.name == "mock"
    assert resolved.upstream_model == "mock"


def test_catalog_passes_mock_settings_on():
    catalog = build_catalog(
        settings(mock_delay_ms=5, mock_output_tokens=9, mock_error_rate=0.5), httpx2.AsyncClient()
    )
    mock = catalog.resolve("mock").provider

    assert (mock.delay_ms, mock.output_tokens, mock.error_rate) == (5, 9, 0.5)


def test_catalog_serves_providers_whose_key_is_set():
    catalog = build_catalog(
        settings(groq_api_key=SecretStr("gsk"), gemini_api_key=SecretStr("gem")),
        httpx2.AsyncClient(),
    )

    groq = catalog.resolve("groq/openai/gpt-oss-20b")
    gemini = catalog.resolve("gemini/gemini-flash-latest")
    assert (groq.provider.name, groq.upstream_model) == ("groq", "openai/gpt-oss-20b")
    assert (gemini.provider.name, gemini.upstream_model) == ("gemini", "gemini-flash-latest")


def test_model_names_can_contain_slashes():
    catalog = build_catalog(settings(groq_api_key=SecretStr("gsk")), httpx2.AsyncClient())

    assert catalog.resolve("groq/meta-llama/llama-4").upstream_model == "meta-llama/llama-4"


@pytest.mark.parametrize(
    "model", ["groq/llama", "gemini/flash", "unknown/model", "groq/", "llama", "mock/x", ""]
)
def test_catalog_does_not_resolve_unserved_models(model: str):
    assert build_catalog(settings(), httpx2.AsyncClient()).resolve(model) is None


def test_empty_catalog_resolves_nothing():
    assert Catalog([]).resolve("mock") is None


class EndlessBody(httpx2.AsyncByteStream):
    """A provider answer that never ends on its own, and records being closed."""

    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self):
        chunk = {"object": "chat.completion.chunk", "choices": [{"delta": {"content": "x"}}]}
        while True:
            yield f"data: {json.dumps(chunk)}\n\n".encode()
            await asyncio.sleep(0)

    async def aclose(self) -> None:
        self.closed = True


def test_closing_a_stream_closes_the_providers_response():
    body = EndlessBody()
    upstream = Upstream(
        lambda req: httpx2.Response(200, stream=body, headers={"Content-Type": "text/event-stream"})
    )

    async def read_one_then_close() -> None:
        chunks = GroqProvider("k", upstream.client()).stream(request())
        await anext(chunks)
        await chunks.aclose()

    asyncio.run(read_one_then_close())

    # The connection is dropped, so the provider stops generating.
    assert body.closed


class BreaksMidStream(httpx2.AsyncByteStream):
    async def __aiter__(self):
        chunk = {"object": "chat.completion.chunk", "choices": [{"delta": {"content": "x"}}]}
        yield f"data: {json.dumps(chunk)}\n\n".encode()
        raise httpx2.ReadError("connection reset")


def test_connection_breaking_mid_stream_is_a_provider_error():
    upstream = Upstream(lambda req: httpx2.Response(200, stream=BreaksMidStream()))

    async def read_all() -> list:
        return await collect(GroqProvider("k", upstream.client()).stream(request()))

    with pytest.raises(ProviderError) as exc_info:
        asyncio.run(read_all())

    assert exc_info.value.retryable


def test_error_body_without_a_message_has_no_upstream_message():
    upstream = Upstream(lambda req: httpx2.Response(500, json={"error": {"code": 42}}))

    with pytest.raises(ProviderError) as exc_info:
        asyncio.run(GroqProvider("k", upstream.client()).complete(request()))

    assert exc_info.value.upstream_message is None
