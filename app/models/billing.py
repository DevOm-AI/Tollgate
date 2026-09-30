import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Money is whole micro-dollars (1 USD = 1,000,000) in BIGINT columns, never floats.

# A calendar month in UTC, e.g. 2026-10.
PERIOD_FORMAT_CHECK = r"period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'"


class ModelPrice(Base):
    """What a provider charges for a model, per 1,000 tokens."""

    __tablename__ = "model_prices"
    __table_args__ = (
        CheckConstraint("input_micros_per_1k >= 0", name="input_price_not_negative"),
        CheckConstraint("output_micros_per_1k >= 0", name="output_price_not_negative"),
    )

    # The same model name can be served (and priced) differently by each provider.
    provider: Mapped[str] = mapped_column(String(50), primary_key=True)
    model: Mapped[str] = mapped_column(String(200), primary_key=True)
    input_micros_per_1k: Mapped[int] = mapped_column(BigInteger)
    output_micros_per_1k: Mapped[int] = mapped_column(BigInteger)


class KeySpend(Base):
    """A key's spend in one month. Budget checks reserve against this row."""

    __tablename__ = "key_spend"
    __table_args__ = (
        CheckConstraint(PERIOD_FORMAT_CHECK, name="period_format"),
        CheckConstraint("spent_micros >= 0", name="spent_not_negative"),
        CheckConstraint("reserved_micros >= 0", name="reserved_not_negative"),
    )

    key_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("api_keys.id", ondelete="CASCADE"), primary_key=True
    )
    period: Mapped[str] = mapped_column(String(7), primary_key=True)
    spent_micros: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    reserved_micros: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")


class Reservation(Base):
    """Money held for one in-flight request, released or settled when it ends."""

    __tablename__ = "reservations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["key_id", "period"],
            ["key_spend.key_id", "key_spend.period"],
            ondelete="CASCADE",
        ),
        CheckConstraint(PERIOD_FORMAT_CHECK, name="period_format"),
        CheckConstraint("amount_micros >= 0", name="amount_not_negative"),
        # The sweeper only looks at open reservations, oldest expiry first.
        Index(
            "ix_reservations_open_expires_at",
            "expires_at",
            postgresql_where=text("settled_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    key_id: Mapped[uuid.UUID]
    period: Mapped[str] = mapped_column(String(7))
    amount_micros: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[datetime]
    settled_at: Mapped[datetime | None]


class UsageOutbox(Base):
    """Usage waiting to be sent to Stripe. Written in the same transaction as the settle."""

    __tablename__ = "usage_outbox"
    __table_args__ = (
        CheckConstraint("cost_micros >= 0", name="cost_not_negative"),
        # The Stripe push only looks at unsent rows.
        Index(
            "ix_usage_outbox_unsent_window_start",
            "customer_id",
            "window_start",
            postgresql_where=text("sent_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    cost_micros: Mapped[int] = mapped_column(BigInteger)
    # The minute the usage belongs to; Stripe deduplicates on customer + minute.
    window_start: Mapped[datetime]
    sent_at: Mapped[datetime | None]
