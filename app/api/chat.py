import math
from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import OpenAIError
from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.core.security import bearer, hash_api_key
from app.limits.rate_limit import RateLimited, RateLimiter, get_rate_limiter
from app.models import ApiKey
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
    key: Annotated[ApiKey, Depends(require_customer_key)],
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
    upstream = body.for_upstream(
        resolved.upstream_model, body.output_cap(settings.default_max_tokens), stream=False
    )
    # Worst case for tokens per minute: the whole prompt plus a full-length answer.
    estimated_tokens = estimate_prompt_tokens(upstream) + upstream.max_tokens
    try:
        admission = await limiter.admit(key.id, key.rpm_limit, key.tpm_limit, estimated_tokens)
    except RateLimited as exc:
        raise _rate_limited(exc) from exc

    # A failed call used no tokens, so the whole estimate goes back.
    actual_tokens = 0
    try:
        completion = await provider.complete(upstream)
        actual_tokens = completion.usage.total_tokens
        return completion
    except ProviderError as exc:
        raise _provider_failed(provider.name, exc) from exc
    finally:
        await limiter.settle(admission, actual_tokens)


def _rate_limited(exc: RateLimited) -> OpenAIError:
    limit = exc.result.limit
    if exc.result.retry_after_ms is None:
        message = (
            f"Request too large: it may use up to {exc.cost} tokens, but the limit is {limit} "
            "tokens per minute. Lower max_tokens or shorten the prompt."
        )
    else:
        wait = math.ceil(exc.result.retry_after_ms / 1000)
        message = f"Rate limit reached: {limit} {exc.limit} per minute. Try again in {wait}s."
    return OpenAIError(
        status.HTTP_429_TOO_MANY_REQUESTS, message, type=exc.limit, code="rate_limit_exceeded"
    )


def _provider_failed(provider: str, exc: ProviderError) -> OpenAIError:
    if exc.is_client_error:
        # The provider rejected the request itself (e.g. an unknown model); its reason helps
        # the caller fix it, and goes only to the caller that sent the request.
        detail = f": {exc.upstream_message}" if exc.upstream_message else ""
        return OpenAIError(
            status.HTTP_400_BAD_REQUEST,
            f"The provider '{provider}' rejected the request{detail}",
            code="provider_rejected_request",
        )
    return OpenAIError(
        status.HTTP_502_BAD_GATEWAY,
        f"The provider '{provider}' failed to answer",
        type="api_error",
        code="provider_error",
    )
