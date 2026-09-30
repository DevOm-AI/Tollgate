import uuid
from datetime import datetime
from typing import Annotated, Self

import anyio
import stripe
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.stripe_billing import StripeBilling, get_stripe_billing
from app.core.db import get_db
from app.core.security import generate_api_key, require_admin
from app.models import ApiKey, Customer, ModelPrice

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

Db = Annotated[AsyncSession, Depends(get_db)]

# Postgres INTEGER / BIGINT bounds, so an oversized value is a 422, not a database error.
INT32_MAX = 2**31 - 1
INT64_MAX = 2**63 - 1

RpmLimit = Annotated[int, Field(gt=0, le=INT32_MAX, description="Requests per minute")]
TpmLimit = Annotated[int, Field(gt=0, le=INT32_MAX, description="Tokens per minute")]
BudgetMicros = Annotated[
    int, Field(ge=0, le=INT64_MAX, description="Monthly budget in micro-dollars (1 USD = 1e6)")
]


class CustomerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    stripe_customer_id: str | None = Field(default=None, min_length=1, max_length=255)


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    stripe_customer_id: str | None
    created_at: datetime


class KeyCreate(BaseModel):
    rpm_limit: RpmLimit
    tpm_limit: TpmLimit
    monthly_budget_micros: BudgetMicros


class KeyUpdate(BaseModel):
    """Only the fields sent are changed."""

    rpm_limit: RpmLimit | None = None
    tpm_limit: TpmLimit | None = None
    monthly_budget_micros: BudgetMicros | None = None

    @model_validator(mode="after")
    def _check_fields(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Send at least one limit to change")
        nulls = sorted(name for name in self.model_fields_set if getattr(self, name) is None)
        if nulls:
            raise ValueError(f"Limits can't be null: {', '.join(nulls)}")
        return self


class KeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    customer_id: uuid.UUID
    prefix: str = Field(description="First characters after tg_live_, for display only")
    rpm_limit: int
    tpm_limit: int
    monthly_budget_micros: int
    is_active: bool
    created_at: datetime


class ModelPriceIn(BaseModel):
    """A provider's price for a model, per 1,000 tokens, in micro-dollars (1 USD = 1e6)."""

    model_config = ConfigDict(from_attributes=True)

    provider: str = Field(min_length=1, max_length=50, examples=["groq"])
    model: str = Field(min_length=1, max_length=200, description="The provider's own model name")
    input_micros_per_1k: Annotated[int, Field(ge=0, le=INT64_MAX)]
    output_micros_per_1k: Annotated[int, Field(ge=0, le=INT64_MAX)]


class CustomerBillingOut(BaseModel):
    stripe_customer_id: str
    subscription_id: str
    price_id: str


class KeyCreated(KeyOut):
    key: str = Field(description="The full key. Shown only in this response; store it now.")


@router.post("/customers", status_code=status.HTTP_201_CREATED)
async def create_customer(body: CustomerCreate, db: Db) -> CustomerOut:
    customer = Customer(name=body.name, stripe_customer_id=body.stripe_customer_id)
    db.add(customer)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A customer with this stripe_customer_id already exists",
        ) from exc
    await db.refresh(customer)
    return CustomerOut.model_validate(customer)


@router.post("/customers/{customer_id}/billing")
async def set_up_billing(
    customer_id: uuid.UUID,
    db: Db,
    billing: Annotated[StripeBilling | None, Depends(get_stripe_billing)],
) -> CustomerBillingOut:
    """Give the customer a Stripe customer and a subscription to the usage price, so their
    usage is billed. Safe to call again: it finds what already exists."""
    if billing is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Stripe isn't set up: STRIPE_SECRET_KEY is not set",
        )
    customer = await db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    try:
        result = await anyio.to_thread.run_sync(
            billing.set_up_customer, customer.id, customer.name, customer.stripe_customer_id
        )
    except stripe.StripeError as exc:
        # Stripe's message says what's wrong (e.g. a bad key); no secrets are in it.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Stripe refused the setup: {exc.user_message or type(exc).__name__}",
        ) from exc
    if customer.stripe_customer_id != result.stripe_customer_id:
        customer.stripe_customer_id = result.stripe_customer_id
        await db.commit()
    return CustomerBillingOut(
        stripe_customer_id=result.stripe_customer_id,
        subscription_id=result.subscription_id,
        price_id=result.price_id,
    )


@router.post("/customers/{customer_id}/keys", status_code=status.HTTP_201_CREATED)
async def create_key(customer_id: uuid.UUID, body: KeyCreate, db: Db) -> KeyCreated:
    if await db.get(Customer, customer_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    new_key = generate_api_key()
    key = ApiKey(
        customer_id=customer_id,
        key_hash=new_key.key_hash,
        prefix=new_key.prefix,
        **body.model_dump(),
    )
    db.add(key)
    await db.commit()
    await db.refresh(key)
    return KeyCreated(**KeyOut.model_validate(key).model_dump(), key=new_key.key)


@router.patch("/keys/{key_id}")
async def update_key(key_id: uuid.UUID, body: KeyUpdate, db: Db) -> KeyOut:
    key = await _get_key(db, key_id)
    for name, value in body.model_dump(exclude_unset=True).items():
        setattr(key, name, value)
    await db.commit()
    return KeyOut.model_validate(key)


@router.post("/keys/{key_id}/revoke")
async def revoke_key(key_id: uuid.UUID, db: Db) -> KeyOut:
    """Deactivate a key for good. Its history (spend, requests) is kept."""
    key = await _get_key(db, key_id)
    key.is_active = False
    await db.commit()
    return KeyOut.model_validate(key)


@router.put("/prices")
async def set_price(body: ModelPriceIn, db: Db) -> ModelPriceIn:
    """Add or change a model's price. Requests for a model with no price are refused."""
    prices = body.model_dump(include={"input_micros_per_1k", "output_micros_per_1k"})
    await db.execute(
        insert(ModelPrice)
        .values(**body.model_dump())
        .on_conflict_do_update(index_elements=["provider", "model"], set_=prices)
    )
    await db.commit()
    return body


@router.get("/prices")
async def list_prices(db: Db) -> list[ModelPriceIn]:
    rows = await db.scalars(select(ModelPrice).order_by(ModelPrice.provider, ModelPrice.model))
    return [ModelPriceIn.model_validate(row) for row in rows]


async def _get_key(db: AsyncSession, key_id: uuid.UUID) -> ApiKey:
    key = await db.get(ApiKey, key_id)
    if key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Key not found")
    return key
