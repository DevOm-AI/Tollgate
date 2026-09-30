import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Identity, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class RequestLog(Base):
    """One proxied request. Metadata only: prompts and answers are never stored."""

    __tablename__ = "requests"
    __table_args__ = (
        CheckConstraint("input_tokens >= 0", name="input_tokens_not_negative"),
        CheckConstraint("output_tokens >= 0", name="output_tokens_not_negative"),
        CheckConstraint("cost_micros >= 0", name="cost_not_negative"),
        # The dashboard reads a key's requests newest first.
        Index("ix_requests_key_id_created_at", "key_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    key_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("api_keys.id", ondelete="CASCADE"))
    # The model the customer asked for.
    model: Mapped[str] = mapped_column(String(200))
    # The provider that answered; empty if none did.
    provider: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(32))
    input_tokens: Mapped[int] = mapped_column(default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(default=0, server_default="0")
    cost_micros: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    latency_ms: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
