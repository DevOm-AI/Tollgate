"""seed the mock model price

Revision ID: 347708ff1f1d
Revises: df9f5eb6c4dc
Create Date: 2026-09-30 15:49:14.272977

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "347708ff1f1d"
down_revision: str | Sequence[str] | None = "df9f5eb6c4dc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The mock provider costs Tollgate nothing, but it's priced like a small real model so its
# requests use budgets the same way (demo and tests). Micro-dollars per 1,000 tokens:
# $0.10 per million input tokens, $0.40 per million output tokens.
MOCK_PRICE = {
    "provider": "mock",
    "model": "mock",
    "input_micros_per_1k": 100,
    "output_micros_per_1k": 400,
}

model_prices = sa.table(
    "model_prices",
    sa.column("provider", sa.String),
    sa.column("model", sa.String),
    sa.column("input_micros_per_1k", sa.BigInteger),
    sa.column("output_micros_per_1k", sa.BigInteger),
)


def upgrade() -> None:
    op.bulk_insert(model_prices, [MOCK_PRICE])


def downgrade() -> None:
    op.execute(
        model_prices.delete().where(
            model_prices.c.provider == "mock", model_prices.c.model == "mock"
        )
    )
