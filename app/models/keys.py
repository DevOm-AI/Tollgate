import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Customer(Base):
    """A customer of the app that uses Tollgate. Billed through Stripe."""

    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ApiKey(Base):
    """A customer's key. Only its hash is stored; the full key is shown once, at creation."""

    __tablename__ = "api_keys"
    __table_args__ = (
        CheckConstraint("rpm_limit > 0", name="rpm_limit_positive"),
        CheckConstraint("tpm_limit > 0", name="tpm_limit_positive"),
        CheckConstraint("monthly_budget_micros >= 0", name="budget_not_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    # Hex SHA-256 of the full key: lookups hash the presented key and match on this.
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # The first characters after tg_live_, so the dashboard can show tg_live_ab12cd34...
    prefix: Mapped[str] = mapped_column(String(8))
    rpm_limit: Mapped[int]
    tpm_limit: Mapped[int]
    # Micro-dollars: 1 USD = 1,000,000.
    monthly_budget_micros: Mapped[int] = mapped_column(BigInteger)
    is_active: Mapped[bool] = mapped_column(default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
