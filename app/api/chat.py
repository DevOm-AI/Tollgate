import time
from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, Response, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import OpenAIError
from app.billing.budget import Outcome, reserve, settle
from app.billing.pricing import cost_micros, max_prompt_tokens
from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.core.security import bearer, hash_api_key
from app.limits.rate_limit import (
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
    ProviderError,
    estimate_prompt_tokens,
)
from app.providers.catalog import Catalog, get_catalog

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


@router.post("/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    response: Response,
    key: Annotated[ApiKey, Depends(require_customer_key)],
    db: Annotated[AsyncSession, Depends(get_db)],
    catalog: Annotated[Catalog, Depends(get_catalog)],
    settings: Annotated[Settings, Depends(get_settings)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> ChatCompletion:
    """Same request and response as OpenAI's POST /v1/chat/completions."""
    if body.stream:
        raise OpenAIError(status.HTTP_400_BAD_REQUEST, "Streaming is not supported", param="stream")
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
        resolved.upstream_model, body.output_cap(settings.default_max_tokens), stream=False
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

    started = time.perf_counter()
    completion: ChatCompletion | None = None
    failure: ProviderError | None = None
    status_ = "error"  # Anything unexpected, until known otherwise.
    try:
        completion = await provider.complete(upstream)
        status_ = "ok"
    except ProviderError as exc:
        failure = exc
        status_ = "provider_error"
    except anyio.get_cancelled_exc_class():
        status_ = "cancelled"
        raise
    finally:
        # Every way out settles: an answer is charged, anything else releases the hold and
        # bills nothing. Shielded, so a cancelled request (e.g. a shutdown) still finishes
        # this instead of leaving the money held until the sweep.
        with anyio.CancelScope(shield=True):
            usage = completion.usage if completion else None
            await settle(
                db,
                hold,
                cost_micros(
                    usage.prompt_tokens,
                    usage.completion_tokens,
                    price.input_micros_per_1k,
                    price.output_micros_per_1k,
                )
                if usage
                else 0,
                Outcome(
                    model=body.model,
                    provider=provider.name,
                    status=status_,
                    input_tokens=usage.prompt_tokens if usage else 0,
                    output_tokens=usage.completion_tokens if usage else 0,
                    latency_ms=round((time.perf_counter() - started) * 1000),
                ),
            )
            tokens = await limiter.settle(admission, usage.total_tokens if usage else 0)
    headers = rate_limit_headers(admission.requests, tokens)
    if failure is not None:
        raise _provider_failed(provider.name, failure, headers) from failure
    response.headers.update(headers)
    return completion


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


def _provider_failed(provider: str, exc: ProviderError, headers: dict[str, str]) -> OpenAIError:
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
