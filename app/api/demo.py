"""The public playground: anyone may try Tollgate with the demo key, without holding it.

The demo key stays on the server. /demo/chat sends a visitor's prompt through the normal
pipeline (rate limits, budget reservation, streaming, settle) as that key, with the model,
the answer length and the prompt size fixed here, not by the visitor. The key's own small
budget and rate limit are what keep a public demo safe to leave running: Tollgate enforces
them like for any customer.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.chat import chat_completions
from app.billing.budget import current_period
from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.core.security import hash_api_key
from app.limits.rate_limit import RateLimiter, get_rate_limiter
from app.models import ApiKey, KeySpend, ModelPrice
from app.providers.base import ChatCompletionRequest
from app.providers.breaker import Breakers, get_breakers
from app.providers.catalog import Catalog, get_catalog

router = APIRouter(prefix="/demo", tags=["demo"])

Db = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


class DemoStatus(BaseModel):
    model: str
    max_tokens: int
    prompt_max_chars: int
    budget_micros: int
    spent_micros: int
    reserved_micros: int
    remaining_micros: int
    rpm_limit: int
    requests_left: int | None  # None if it can't be read right now (Redis down).
    # The demo model's price, so the page can show what each answer cost. None if the model
    # is a route rather than a single priced model.
    input_micros_per_1k: int | None
    output_micros_per_1k: int | None


class DemoPrompt(BaseModel):
    prompt: str = Field(min_length=1)


async def demo_key(db: Db, settings: AppSettings) -> ApiKey:
    if settings.demo_api_key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="The playground isn't set up")
    key = await db.scalar(
        select(ApiKey).where(
            ApiKey.key_hash == hash_api_key(settings.demo_api_key.get_secret_value())
        )
    )
    if key is None or not key.is_active:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="The demo key is missing or revoked"
        )
    return key


DemoKey = Annotated[ApiKey, Depends(demo_key)]


@router.get("/status")
async def demo_status(
    key: DemoKey,
    db: Db,
    settings: AppSettings,
    catalog: Annotated[Catalog, Depends(get_catalog)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> DemoStatus:
    spend = await db.get(KeySpend, (key.id, current_period()))
    spent = spend.spent_micros if spend else 0
    reserved = spend.reserved_micros if spend else 0
    resolved = catalog.resolve(settings.demo_model)
    price = (
        await db.get(ModelPrice, (resolved.provider.name, resolved.upstream_model))
        if resolved
        else None
    )
    return DemoStatus(
        model=settings.demo_model,
        max_tokens=settings.demo_max_tokens,
        prompt_max_chars=settings.demo_prompt_max_chars,
        budget_micros=key.monthly_budget_micros,
        spent_micros=spent,
        reserved_micros=reserved,
        remaining_micros=max(0, key.monthly_budget_micros - spent - reserved),
        rpm_limit=key.rpm_limit,
        requests_left=await limiter.requests_left(key.id, key.rpm_limit),
        input_micros_per_1k=price.input_micros_per_1k if price else None,
        output_micros_per_1k=price.output_micros_per_1k if price else None,
    )


@router.post("/chat")
async def demo_chat(
    body: DemoPrompt,
    response: Response,
    key: DemoKey,
    db: Db,
    settings: AppSettings,
    catalog: Annotated[Catalog, Depends(get_catalog)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    breakers: Annotated[Breakers, Depends(get_breakers)],
):
    """Stream an answer to `prompt` as the demo key: OpenAI chat.completion.chunk events,
    ending with a usage chunk, and the demo key's rate limits in the headers."""
    if len(body.prompt) > settings.demo_prompt_max_chars:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"The prompt is limited to {settings.demo_prompt_max_chars} characters",
        )
    request = ChatCompletionRequest.model_validate(
        {
            "model": settings.demo_model,
            "messages": [{"role": "user", "content": body.prompt}],
            "max_tokens": settings.demo_max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    )
    return await chat_completions(request, response, key, db, catalog, settings, limiter, breakers)
