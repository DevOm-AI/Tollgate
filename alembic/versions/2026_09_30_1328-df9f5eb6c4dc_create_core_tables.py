"""create core tables

Revision ID: df9f5eb6c4dc
Create Date: 2026-09-30 13:28:15.016396

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "df9f5eb6c4dc"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("stripe_customer_id", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customers")),
        sa.UniqueConstraint("stripe_customer_id", name=op.f("uq_customers_stripe_customer_id")),
    )
    op.create_table(
        "model_prices",
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("input_micros_per_1k", sa.BigInteger(), nullable=False),
        sa.Column("output_micros_per_1k", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "input_micros_per_1k >= 0", name=op.f("ck_model_prices_input_price_not_negative")
        ),
        sa.CheckConstraint(
            "output_micros_per_1k >= 0", name=op.f("ck_model_prices_output_price_not_negative")
        ),
        sa.PrimaryKeyConstraint("provider", "model", name=op.f("pk_model_prices")),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=8), nullable=False),
        sa.Column("rpm_limit", sa.Integer(), nullable=False),
        sa.Column("tpm_limit", sa.Integer(), nullable=False),
        sa.Column("monthly_budget_micros", sa.BigInteger(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "monthly_budget_micros >= 0", name=op.f("ck_api_keys_budget_not_negative")
        ),
        sa.CheckConstraint("rpm_limit > 0", name=op.f("ck_api_keys_rpm_limit_positive")),
        sa.CheckConstraint("tpm_limit > 0", name=op.f("ck_api_keys_tpm_limit_positive")),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_api_keys_customer_id_customers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("key_hash", name=op.f("uq_api_keys_key_hash")),
    )
    op.create_index(op.f("ix_api_keys_customer_id"), "api_keys", ["customer_id"], unique=False)
    op.create_table(
        "usage_outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("cost_micros", sa.BigInteger(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("cost_micros >= 0", name=op.f("ck_usage_outbox_cost_not_negative")),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_usage_outbox_customer_id_customers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_outbox")),
    )
    op.create_index(
        "ix_usage_outbox_unsent_window_start",
        "usage_outbox",
        ["customer_id", "window_start"],
        unique=False,
        postgresql_where=sa.text("sent_at IS NULL"),
    )
    op.create_table(
        "key_spend",
        sa.Column("key_id", sa.Uuid(), nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("spent_micros", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("reserved_micros", sa.BigInteger(), server_default="0", nullable=False),
        sa.CheckConstraint(
            "period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'", name=op.f("ck_key_spend_period_format")
        ),
        sa.CheckConstraint("reserved_micros >= 0", name=op.f("ck_key_spend_reserved_not_negative")),
        sa.CheckConstraint("spent_micros >= 0", name=op.f("ck_key_spend_spent_not_negative")),
        sa.ForeignKeyConstraint(
            ["key_id"],
            ["api_keys.id"],
            name=op.f("fk_key_spend_key_id_api_keys"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("key_id", "period", name=op.f("pk_key_spend")),
    )
    op.create_table(
        "requests",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("key_id", sa.Uuid(), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_micros", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("cost_micros >= 0", name=op.f("ck_requests_cost_not_negative")),
        sa.CheckConstraint("input_tokens >= 0", name=op.f("ck_requests_input_tokens_not_negative")),
        sa.CheckConstraint(
            "output_tokens >= 0", name=op.f("ck_requests_output_tokens_not_negative")
        ),
        sa.ForeignKeyConstraint(
            ["key_id"],
            ["api_keys.id"],
            name=op.f("fk_requests_key_id_api_keys"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_requests")),
    )
    op.create_index(
        "ix_requests_key_id_created_at", "requests", ["key_id", "created_at"], unique=False
    )
    op.create_table(
        "reservations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key_id", sa.Uuid(), nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("amount_micros", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'", name=op.f("ck_reservations_period_format")
        ),
        sa.CheckConstraint("amount_micros >= 0", name=op.f("ck_reservations_amount_not_negative")),
        sa.ForeignKeyConstraint(
            ["key_id", "period"],
            ["key_spend.key_id", "key_spend.period"],
            name=op.f("fk_reservations_key_id_key_spend"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reservations")),
    )
    op.create_index(
        "ix_reservations_open_expires_at",
        "reservations",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("settled_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reservations_open_expires_at",
        table_name="reservations",
        postgresql_where=sa.text("settled_at IS NULL"),
    )
    op.drop_table("reservations")
    op.drop_index("ix_requests_key_id_created_at", table_name="requests")
    op.drop_table("requests")
    op.drop_table("key_spend")
    op.drop_index(
        "ix_usage_outbox_unsent_window_start",
        table_name="usage_outbox",
        postgresql_where=sa.text("sent_at IS NULL"),
    )
    op.drop_table("usage_outbox")
    op.drop_index(op.f("ix_api_keys_customer_id"), table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_table("model_prices")
    op.drop_table("customers")
