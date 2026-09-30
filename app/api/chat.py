import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import Receive, Scope, Send

from app.api.errors import OpenAIError
from app.billing.budget import Hold, Outcome, reserve, settle
from app.billing.pricing import cost_micros, max_prompt_tokens
from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.core.security import bearer, hash_api_key
from app.limits.rate_limit import (
    Admission,
    RateLimited,
    RateLimiter,
    get_rate_limiter,
    rate_limit_headers,
    retry_after,
)
from app.models import ApiKey, ModelPrice
from app.providers.base import (
    ChatCompletion,
    ChatCompletionRequest,
    Provider,
    ProviderError,
    ProviderTimeout,
    estimate_prompt_tokens,
)
from app.providers.catalog import Catalog, get_catalog
from app.providers.tokens import count_usage

router = APIRouter(prefix="/v1", tags=["openai"])


async def require_customer_key(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApiKey:
    """The active key the request was sent with. Looked up by hash; the key itself isn't kept."""
    if credentials is None:
        raise _invalid_key("Missing API key. Send it as: Authorization: Bearer tg_live_...")
    key = await db.scalar(
        select(ApiKey).where(ApiKey.key_hash == hash_api_key(credentials.credentials))
    )
    if key is None or not key.is_active:
        raise _invalid_key("Incorrect or revoked API key")
    return key


def _invalid_key(message: str) -> OpenAIError:
    return OpenAIError(
        status.HTTP_401_UNAUTHORIZED,
        message,
        code="invalid_api_key",
        headers={"WWW-Authenticate": "Bearer"},
    )


@dataclass
class GatedCall:
    """A request that passed every gate (price, rate limits, budget) and holds its money.

    Every way out must call `finish` exactly once, which settles both the budget and the
    rate limits.
    """

    db: AsyncSession
    limiter: RateLimiter
    provider: Provider
    upstream: ChatCompletionRequest
    price: ModelPrice
    admission: Admission
    hold: Hold
    model: str  # As the customer asked for it.
    started: float
    first_token_timeout_s: float
    deadline: float  # anyio.current_time() by which the whole answer must be in.

    def time_left(self) -> float:
        return max(0.0, self.deadline - anyio.current_time())

    def first_token_time_left(self) -> float:
        return min(self.first_token_timeout_s, self.time_left())

    async def finish(
        self, status_: str, input_tokens: int = 0, output_tokens: int = 0
    ) -> dict[str, str]:
        """Charge what was used, release the rest, and return the rate limit headers.

        Shielded, so a cancelled request (client gone, shutdown) still settles instead of
        leaving the money held until the sweep.
        """
        with anyio.CancelScope(shield=True):
            await settle(
                self.db,
                self.hold,
                cost_micros(
                    input_tokens,
                    output_tokens,
                    self.price.input_micros_per_1k,
                    self.price.output_micros_per_1k,
                ),
                Outcome(
                    model=self.model,
                    provider=self.provider.name,
                    status=status_,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=round((time.perf_counter() - self.started) * 1000),
                ),
            )
            tokens = await self.limiter.settle(self.admission, input_tokens + output_tokens)
        return rate_limit_headers(self.admission.requests, tokens)


@router.post("/chat/completions", response_model=ChatCompletion)
async def chat_completions(
    body: ChatCompletionRequest,
    response: Response,
    key: Annotated[ApiKey, Depends(require_customer_key)],
    db: Annotated[AsyncSession, Depends(get_db)],
    catalog: Annotated[Catalog, Depends(get_catalog)],
    settings: Annotated[Settings, Depends(get_settings)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> ChatCompletion | StreamingResponse:
    """Same request and response as OpenAI's POST /v1/chat/completions, streaming included."""
    call = await _open_call(body, key, db, catalog, settings, limiter)
    if body.stream:
        return await _stream(call, body)

    completion: ChatCompletion | None = None
    failure: ProviderError | None = None
    status_ = "error"  # Anything unexpected, until known otherwise.
    try:
        with anyio.fail_after(call.time_left()):
            completion = await call.provider.complete(call.upstream)
        status_ = "ok"
    except TimeoutError:
        failure = ProviderTimeout(f"{call.provider.name}: no answer in time")
        status_ = "timeout"
    except ProviderError as exc:
        failure = exc
        status_ = _failure_status(exc)
    except anyio.get_cancelled_exc_class():
        status_ = "cancelled"
        raise
    finally:
        # An answer is charged; anything else releases the hold and bills nothing.
        usage = completion.usage if completion else None
        headers = await call.finish(
            status_,
            usage.prompt_tokens if usage else 0,
            usage.completion_tokens if usage else 0,
        )
    if failure is not None:
        raise _provider_failed(call.provider.name, failure, headers) from failure
    response.headers.update(headers)
    return completion


async def _open_call(
    body: ChatCompletionRequest,
    key: ApiKey,
    db: AsyncSession,
    catalog: Catalog,
    settings: Settings,
    limiter: RateLimiter,
) -> GatedCall:
    """Find the provider and price, then pass the rate limits and reserve the budget."""
    resolved = catalog.resolve(body.model)
    if resolved is None:
        raise OpenAIError(
            status.HTTP_404_NOT_FOUND,
            f"The model '{body.model}' does not exist",
            code="model_not_found",
            param="model",
        )
    provider = resolved.provider
    price = await db.get(ModelPrice, (provider.name, resolved.upstream_model))
    if price is None:
        # Without a price the request can't be budgeted, so it never reaches the provider.
        raise OpenAIError(
            status.HTTP_400_BAD_REQUEST,
            f"The model '{body.model}' has no price set, so it can't be billed",
            code="model_not_priced",
            param="model",
        )
    upstream = body.for_upstream(
        resolved.upstream_model, body.output_cap(settings.default_max_tokens), stream=body.stream
    )
    if body.stream:
        # Ask for usage in the last chunk, so the stream can be billed from real counts.
        options = (body.model_extra or {}).get("stream_options") or {}
        upstream = upstream.model_copy(
            update={"stream_options": {**options, "include_usage": True}}
        )

    # Rate limits first: they're cheap (Redis) and keep floods off Postgres.
    # Worst case for tokens per minute: the whole prompt plus a full-length answer.
    estimated_tokens = estimate_prompt_tokens(upstream) + upstream.max_tokens
    try:
        admission = await limiter.admit(key.id, key.rpm_limit, key.tpm_limit, estimated_tokens)
    except RateLimited as exc:
        raise _rate_limited(exc) from exc

    # Then the budget: hold the most this request could cost before spending anything.
    worst_case = cost_micros(
        max_prompt_tokens(upstream),
        upstream.max_tokens,
        price.input_micros_per_1k,
        price.output_micros_per_1k,
    )
    hold = await reserve(db, key, worst_case)
    if hold is None:
        # Not going ahead, so it uses none of the tokens it took.
        tokens = await limiter.settle(admission, 0)
        raise _budget_exceeded(worst_case, rate_limit_headers(admission.requests, tokens))

    return GatedCall(
        db=db,
        limiter=limiter,
        provider=provider,
        upstream=upstream,
        price=price,
        admission=admission,
        hold=hold,
        model=body.model,
        started=time.perf_counter(),
        first_token_timeout_s=settings.provider_first_token_timeout_s,
        deadline=anyio.current_time() + settings.provider_total_timeout_s,
    )


# Tell proxies (and the Hugging Face / nginx front) not to buffer: chunks must arrive as sent.
STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


class HangUpAwareStreamingResponse(StreamingResponse):
    """A StreamingResponse that stops the stream the moment the client hangs up.

    Starlette only watches for the disconnect under ASGI spec < 2.4; from 2.4 it waits for a
    send to fail and leaves the generator open, so the provider would keep generating (and
    Tollgate paying) for nobody. This watches under every spec, always closes the generator,
    and then calls `on_close`: closing a generator that never started runs none of its
    code, so `on_close` is what settles a stream that ends before its first chunk is sent.
    """

    def __init__(
        self, content: AsyncIterator[str], on_close: Callable[[], Awaitable[object]], **kwargs
    ) -> None:
        super().__init__(content, **kwargs)
        self.on_close = on_close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            async with anyio.create_task_group() as task_group:

                async def stream_then_stop() -> None:
                    await self.stream_response(send)
                    task_group.cancel_scope.cancel()

                task_group.start_soon(stream_then_stop)
                await self.listen_for_disconnect(receive)
                task_group.cancel_scope.cancel()
        except* OSError:
            pass  # A send failed: the client is gone.
        finally:
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()
                await self.on_close()


class OpenStream:
    """A provider stream that's being forwarded, and the one place it's closed and settled."""

    def __init__(
        self, call: GatedCall, chunks: AsyncIterator[dict[str, Any]], tally: "StreamTally"
    ):
        self.call = call
        self.chunks = chunks
        self.tally = tally
        self.closed = False

    async def close(self, status_: str) -> dict[str, str]:
        """Stop the provider's stream (no paying for tokens nobody reads), then settle, and
        return the rate limit headers. Only the first call does anything.

        Shielded throughout: counting and settling must finish even if the client is gone.
        """
        if self.closed:
            return {}
        self.closed = True
        with anyio.CancelScope(shield=True):
            aclose = getattr(self.chunks, "aclose", None)
            if aclose is not None:
                await aclose()
            input_tokens, output_tokens = await self.tally.tokens(self.call.upstream)
            return await self.call.finish(status_, input_tokens, output_tokens)


async def _stream(call: GatedCall, body: ChatCompletionRequest) -> StreamingResponse:
    """Forward the provider's chunks as Server-Sent Events, as they arrive.

    The first chunk is read before answering, so a provider that fails before any output
    still gets a proper error status (and the hold is released). After that the status is
    already 200: a failure mid-stream is sent as an error event, and what was generated
    until then is billed.
    """
    options = (body.model_extra or {}).get("stream_options") or {}
    tally = StreamTally(show_usage=bool(options.get("include_usage")))
    stream = OpenStream(call, call.provider.stream(call.upstream), tally)
    try:
        with anyio.fail_after(call.first_token_time_left()):
            first = await anext(stream.chunks)
    except StopAsyncIteration:
        first = None
    except TimeoutError as exc:
        headers = await stream.close("timeout")
        timeout = ProviderTimeout(f"{call.provider.name}: no first token in time")
        raise _provider_failed(call.provider.name, timeout, headers) from exc
    except ProviderError as exc:
        headers = await stream.close(_failure_status(exc))
        raise _provider_failed(call.provider.name, exc, headers) from exc
    except BaseException as exc:
        cancelled = isinstance(exc, anyio.get_cancelled_exc_class())
        await stream.close("cancelled" if cancelled else "error")
        raise
    # Counted now: the provider generated it, so it's billed even if it's never sent.
    first_events = tally.forward(first) if first is not None else []

    return HangUpAwareStreamingResponse(
        _forward(stream, first_events),
        on_close=lambda: stream.close("cancelled"),
        media_type="text/event-stream",
        headers=rate_limit_headers(call.admission.requests, call.admission.tokens) | STREAM_HEADERS,
    )


async def _forward(stream: OpenStream, first_events: list[str]) -> AsyncIterator[str]:
    status_ = "error"  # Anything unexpected, until known otherwise.
    try:
        for event in first_events:
            yield event
        while True:
            # The deadline covers only the wait for the next chunk, never a yield: a cancel
            # scope open across a yield breaks when the response closes this generator.
            try:
                with anyio.fail_after(stream.call.time_left()):
                    chunk = await anext(stream.chunks)
            except StopAsyncIteration:
                break
            for event in stream.tally.forward(chunk):
                yield event
        status_ = "ok"
        yield "data: [DONE]\n\n"
    except (ProviderError, TimeoutError) as exc:
        timed_out = isinstance(exc, TimeoutError | ProviderTimeout)
        status_ = "timeout" if timed_out else "provider_error"
        name = stream.call.provider.name
        yield _sse(
            {
                "error": {
                    "message": f"The provider '{name}' "
                    + ("didn't finish in time" if timed_out else "failed mid-answer"),
                    "type": "api_error",
                    "param": None,
                    "code": "provider_timeout" if timed_out else "provider_error",
                }
            }
        )
    except (anyio.get_cancelled_exc_class(), GeneratorExit):
        status_ = "cancelled"  # The client hung up.
        raise
    finally:
        await stream.close(status_)


class StreamTally:
    """Counts what a stream used, and shapes each chunk for the client."""

    def __init__(self, *, show_usage: bool) -> None:
        # Usage is always requested from the provider; the client only sees it if it asked.
        self.show_usage = show_usage
        self.usage: dict[str, Any] | None = None
        self.output: list[str] = []

    def forward(self, chunk: dict[str, Any]) -> list[str]:
        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                self.output.append(delta["content"])
            for tool_call in delta.get("tool_calls") or []:
                arguments = (tool_call.get("function") or {}).get("arguments")
                if isinstance(arguments, str):
                    self.output.append(arguments)
        if not self.show_usage and "usage" in chunk:
            chunk = {name: value for name, value in chunk.items() if name != "usage"}
            if not chunk.get("choices"):
                return []  # The usage-only last chunk the client didn't ask for.
        return [_sse(chunk)]

    async def tokens(self, request: ChatCompletionRequest) -> tuple[int, int]:
        """(input, output) tokens: the provider's count if it sent one, else our own count
        of the prompt and of what was streamed (e.g. a stream cut before its usage chunk).

        Nothing generated means nothing billed: the provider failed before answering.
        """
        if self.usage is not None:
            return int(self.usage.get("prompt_tokens") or 0), int(
                self.usage.get("completion_tokens") or 0
            )
        if not self.output:
            return 0, 0
        prompt, output = await count_usage(request, "".join(self.output))
        # The provider can't have generated more than it was allowed to.
        return prompt, min(output, request.max_tokens or output)


def _sse(data: dict[str, Any]) -> str:
    return f"data: {json.dumps(data, separators=(',', ':'))}\n\n"


def _budget_exceeded(worst_case: int, headers: dict[str, str]) -> OpenAIError:
    return OpenAIError(
        status.HTTP_402_PAYMENT_REQUIRED,
        "Monthly budget exceeded: this request could cost up to "
        f"{worst_case} micro-dollars and the key's remaining budget can't cover it. "
        "Lower max_tokens or raise the budget.",
        type="insufficient_quota",
        code="budget_exceeded",
        headers=headers,
    )


def _rate_limited(exc: RateLimited) -> OpenAIError:
    limit = exc.result.limit
    headers = rate_limit_headers(exc.requests, exc.tokens)
    wait = retry_after(exc)
    if wait is None:
        # Retrying can't help, so there's no Retry-After.
        message = (
            f"Request too large: it may use up to {exc.cost} tokens, but the limit is {limit} "
            "tokens per minute. Lower max_tokens or shorten the prompt."
        )
    else:
        headers["Retry-After"] = str(wait)
        message = f"Rate limit reached: {limit} {exc.limit} per minute. Try again in {wait}s."
    return OpenAIError(
        status.HTTP_429_TOO_MANY_REQUESTS,
        message,
        type=exc.limit,
        code="rate_limit_exceeded",
        headers=headers,
    )


def _failure_status(exc: ProviderError) -> str:
    """How a failed call is recorded in the requests table."""
    return "timeout" if isinstance(exc, ProviderTimeout) else "provider_error"


def _provider_failed(provider: str, exc: ProviderError, headers: dict[str, str]) -> OpenAIError:
    if isinstance(exc, ProviderTimeout):
        return OpenAIError(
            status.HTTP_504_GATEWAY_TIMEOUT,
            f"The provider '{provider}' didn't answer in time",
            type="api_error",
            code="provider_timeout",
            headers=headers,
        )
    if exc.is_client_error:
        # The provider rejected the request itself (e.g. an unknown model); its reason helps
        # the caller fix it, and goes only to the caller that sent the request.
        detail = f": {exc.upstream_message}" if exc.upstream_message else ""
        return OpenAIError(
            status.HTTP_400_BAD_REQUEST,
            f"The provider '{provider}' rejected the request{detail}",
            code="provider_rejected_request",
            headers=headers,
        )
    return OpenAIError(
        status.HTTP_502_BAD_GATEWAY,
        f"The provider '{provider}' failed to answer",
        type="api_error",
        code="provider_error",
        headers=headers,
    )
