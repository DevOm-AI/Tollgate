import uuid
from datetime import datetime
from typing import Annotated, Self

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import generate_api_key, require_admin
from app.models import ApiKey, Customer

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


async def _get_key(db: AsyncSession, key_id: uuid.UUID) -> ApiKey:
    key = await db.get(ApiKey, key_id)
    if key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Key not found")
    return key
