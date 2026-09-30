"""Read-only admin routes the dashboard is built on: keys, their stats and request logs.

Everything here is metadata: request logs hold counts, timings and costs, never prompts or
answers.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.budget import current_period
from app.core.db import get_db
from app.core.security import require_admin
from app.models import ApiKey, Customer, KeySpend, RequestLog

router = APIRouter(prefix="/admin", tags=["dashboard"], dependencies=[Depends(require_admin)])

Db = Annotated[AsyncSession, Depends(get_db)]

# Request outcomes that aren't the provider's or Tollgate's fault.
NOT_ERRORS = ("ok", "cancelled")


class KeySummary(BaseModel):
    id: uuid.UUID
    customer_id: uuid.UUID
    customer_name: str
    prefix: str
    rpm_limit: int
    tpm_limit: int
    monthly_budget_micros: int
    is_active: bool
    created_at: datetime
    period: str
    spent_micros: int
    reserved_micros: int


class DayStats(BaseModel):
    day: date
    requests: int
    errors: int
    input_tokens: int
    output_tokens: int
    cost_micros: int


class ProviderStats(BaseModel):
    provider: str
    requests: int
    errors: int
    error_rate: float


class KeyStats(BaseModel):
    key: KeySummary
    days: int
    daily: list[DayStats]
    # Over answered requests in the window; None when there were none.
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    providers: list[ProviderStats]


class RequestOut(BaseModel):
    id: int
    model: str
    provider: str | None
    status: str
    input_tokens: int
    output_tokens: int
    cost_micros: int
    latency_ms: int | None
    created_at: datetime


class CustomerSummary(BaseModel):
    id: uuid.UUID
    name: str
    stripe_customer_id: str | None
    created_at: datetime


@router.get("/customers")
async def list_customers(db: Db) -> list[CustomerSummary]:
    rows = await db.scalars(select(Customer).order_by(Customer.name, Customer.created_at))
    return [CustomerSummary.model_validate(row, from_attributes=True) for row in rows]


@router.get("/keys")
async def list_keys(db: Db) -> list[KeySummary]:
    """Every key, newest first, with this month's spend."""
    return await _key_summaries(db)


@router.get("/keys/{key_id}/stats")
async def key_stats(
    key_id: uuid.UUID, db: Db, days: Annotated[int, Query(ge=1, le=90)] = 30
) -> KeyStats:
    summaries = await _key_summaries(db, key_id)
    if not summaries:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Key not found")
    today = datetime.now(UTC).date()
    since = datetime.combine(today - timedelta(days=days - 1), datetime.min.time(), tzinfo=UTC)
    params = {"key_id": key_id, "since": since, "not_errors": list(NOT_ERRORS)}

    daily = await db.execute(DAILY, params | {"today": today, "first_day": since.date()})
    latency = (await db.execute(LATENCY, params)).one()
    providers = await db.execute(BY_PROVIDER, params)
    return KeyStats(
        key=summaries[0],
        days=days,
        daily=[DayStats(**row._mapping) for row in daily],
        latency_p50_ms=latency.p50,
        latency_p95_ms=latency.p95,
        providers=[
            ProviderStats(
                provider=row.provider,
                requests=row.requests,
                errors=row.errors,
                error_rate=row.errors / row.requests if row.requests else 0.0,
            )
            for row in providers
        ],
    )


@router.get("/keys/{key_id}/requests")
async def key_requests(
    key_id: uuid.UUID,
    db: Db,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before_id: Annotated[int | None, Query(description="Page back from this request id")] = None,
) -> list[RequestOut]:
    """The key's request log, newest first. Metadata only."""
    if await db.get(ApiKey, key_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Key not found")
    query = select(RequestLog).where(RequestLog.key_id == key_id)
    if before_id is not None:
        query = query.where(RequestLog.id < before_id)
    rows = await db.scalars(query.order_by(RequestLog.id.desc()).limit(limit))
    return [RequestOut.model_validate(row, from_attributes=True) for row in rows]


async def _key_summaries(db: AsyncSession, key_id: uuid.UUID | None = None) -> list[KeySummary]:
    period = current_period()
    query = (
        select(ApiKey, Customer.name, KeySpend.spent_micros, KeySpend.reserved_micros)
        .join(Customer, Customer.id == ApiKey.customer_id)
        .outerjoin(KeySpend, (KeySpend.key_id == ApiKey.id) & (KeySpend.period == period))
        .order_by(ApiKey.created_at.desc())
    )
    if key_id is not None:
        query = query.where(ApiKey.id == key_id)
    rows = await db.execute(query)
    return [
        KeySummary(
            id=key.id,
            customer_id=key.customer_id,
            customer_name=customer_name,
            prefix=key.prefix,
            rpm_limit=key.rpm_limit,
            tpm_limit=key.tpm_limit,
            monthly_budget_micros=key.monthly_budget_micros,
            is_active=key.is_active,
            created_at=key.created_at,
            period=period,
            spent_micros=spent or 0,
            reserved_micros=reserved or 0,
        )
        for key, customer_name, spent, reserved in rows
    ]


# Every day in the window (UTC), including days without requests.
DAILY = text(
    """
    SELECT d.day::date AS day,
           count(r.id) AS requests,
           count(r.id) FILTER (WHERE r.status <> ALL(:not_errors)) AS errors,
           coalesce(sum(r.input_tokens), 0) AS input_tokens,
           coalesce(sum(r.output_tokens), 0) AS output_tokens,
           coalesce(sum(r.cost_micros), 0) AS cost_micros
    FROM generate_series(CAST(:first_day AS date), CAST(:today AS date), interval '1 day') AS d(day)
    LEFT JOIN requests AS r
      ON r.key_id = :key_id AND (r.created_at AT TIME ZONE 'UTC')::date = d.day::date
    GROUP BY d.day
    ORDER BY d.day
    """
)

LATENCY = text(
    """
    SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95
    FROM requests
    WHERE key_id = :key_id AND created_at >= :since AND status = 'ok'
      AND latency_ms IS NOT NULL
    """
)

BY_PROVIDER = text(
    """
    SELECT coalesce(provider, 'none') AS provider,
           count(*) AS requests,
           count(*) FILTER (WHERE status <> ALL(:not_errors)) AS errors
    FROM requests
    WHERE key_id = :key_id AND created_at >= :since
    GROUP BY 1
    ORDER BY 1
    """
)
