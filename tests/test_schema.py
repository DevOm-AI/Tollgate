import uuid
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, delete, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ApiKey,
    Base,
    Customer,
    KeySpend,
    ModelPrice,
    RequestLog,
    Reservation,
    UsageOutbox,
)
from tests.conftest import alembic_config

TABLES = {
    "customers",
    "api_keys",
    "model_prices",
    "key_spend",
    "reservations",
    "requests",
    "usage_outbox",
}


def make_key(db: Session, **overrides) -> ApiKey:
    customer = Customer(name="Acme")
    db.add(customer)
    db.flush()
    values = {
        "customer_id": customer.id,
        "key_hash": uuid.uuid4().hex * 2,
        "prefix": "ab12cd34",
        "rpm_limit": 60,
        "tpm_limit": 100_000,
        "monthly_budget_micros": 1_000_000,
    }
    key = ApiKey(**(values | overrides))
    db.add(key)
    db.flush()
    return key


def assert_rejected(db: Session, row: Base, constraint: str) -> None:
    db.add(row)
    with pytest.raises(IntegrityError, match=constraint):
        db.flush()
    db.rollback()


# --- Migrations ---


def test_migrations_create_every_table(db_engine: Engine):
    assert TABLES <= set(inspect(db_engine).get_table_names())


def test_migrations_match_the_models(db_engine: Engine):
    with db_engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []


def test_downgrade_removes_every_table_and_upgrade_restores_them(db_engine: Engine):
    # Postgres DDL is transactional, so rolling back leaves the shared test database as it was.
    with db_engine.connect() as connection, connection.begin() as transaction:
        config = alembic_config(connection)

        command.downgrade(config, "base")
        assert not TABLES & set(inspect(connection).get_table_names())

        command.upgrade(config, "head")
        assert TABLES <= set(inspect(connection).get_table_names())
        transaction.rollback()


# --- Defaults ---


def test_new_key_is_active_with_a_creation_time(db: Session):
    key = make_key(db)
    db.refresh(key)

    assert key.is_active is True
    assert key.created_at is not None


def test_key_spend_starts_at_zero(db: Session):
    key = make_key(db)
    db.add(KeySpend(key_id=key.id, period="2026-10"))
    db.flush()

    spend = db.scalars(select(KeySpend).where(KeySpend.key_id == key.id)).one()
    assert (spend.spent_micros, spend.reserved_micros) == (0, 0)


def test_money_columns_hold_more_than_32_bits(db: Session):
    # 1 million USD in micro-dollars overflows a 32-bit integer.
    key = make_key(db, monthly_budget_micros=1_000_000 * 1_000_000)
    db.expire(key)

    assert key.monthly_budget_micros == 10**12


# --- Constraints ---


def test_key_hash_is_unique(db: Session):
    key = make_key(db)

    assert_rejected(
        db,
        ApiKey(
            customer_id=key.customer_id,
            key_hash=key.key_hash,
            prefix="ef56ab78",
            rpm_limit=60,
            tpm_limit=100_000,
            monthly_budget_micros=0,
        ),
        "uq_api_keys_key_hash",
    )


@pytest.mark.parametrize(
    ("field", "value", "constraint"),
    [
        ("rpm_limit", 0, "ck_api_keys_rpm_limit_positive"),
        ("tpm_limit", 0, "ck_api_keys_tpm_limit_positive"),
        ("monthly_budget_micros", -1, "ck_api_keys_budget_not_negative"),
    ],
)
def test_key_limits_are_checked(db: Session, field: str, value: int, constraint: str):
    customer = Customer(name="Acme")
    db.add(customer)
    db.flush()
    values = {
        "customer_id": customer.id,
        "key_hash": "f" * 64,
        "prefix": "ab12cd34",
        "rpm_limit": 60,
        "tpm_limit": 100_000,
        "monthly_budget_micros": 1_000_000,
    }

    assert_rejected(db, ApiKey(**(values | {field: value})), constraint)


@pytest.mark.parametrize(
    ("field", "constraint"),
    [
        ("spent_micros", "ck_key_spend_spent_not_negative"),
        ("reserved_micros", "ck_key_spend_reserved_not_negative"),
    ],
)
def test_key_spend_cannot_go_negative(db: Session, field: str, constraint: str):
    key = make_key(db)

    assert_rejected(db, KeySpend(key_id=key.id, period="2026-10", **{field: -1}), constraint)


@pytest.mark.parametrize("period", ["2026-13", "2026-00", "2026-1", "26-10", "2026/10"])
def test_period_must_be_a_calendar_month(db: Session, period: str):
    key = make_key(db)

    assert_rejected(db, KeySpend(key_id=key.id, period=period), "ck_key_spend_period_format")


def test_reservation_needs_a_key_spend_row(db: Session):
    key = make_key(db)

    assert_rejected(
        db,
        Reservation(
            key_id=key.id,
            period="2026-10",
            amount_micros=1_000,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        ),
        "fk_reservations_key_id_key_spend",
    )


def test_reservation_amount_cannot_be_negative(db: Session):
    key = make_key(db)
    db.add(KeySpend(key_id=key.id, period="2026-10"))
    db.flush()

    assert_rejected(
        db,
        Reservation(
            key_id=key.id,
            period="2026-10",
            amount_micros=-1,
            expires_at=datetime.now(UTC),
        ),
        "ck_reservations_amount_not_negative",
    )


def test_model_price_cannot_be_negative(db: Session):
    assert_rejected(
        db,
        ModelPrice(provider="groq", model="m", input_micros_per_1k=-1, output_micros_per_1k=0),
        "ck_model_prices_input_price_not_negative",
    )


def test_request_cost_cannot_be_negative(db: Session):
    key = make_key(db)

    assert_rejected(
        db,
        RequestLog(key_id=key.id, model="m", status="ok", cost_micros=-1),
        "ck_requests_cost_not_negative",
    )


def test_usage_cost_cannot_be_negative(db: Session):
    key = make_key(db)

    assert_rejected(
        db,
        UsageOutbox(customer_id=key.customer_id, cost_micros=-1, window_start=datetime.now(UTC)),
        "ck_usage_outbox_cost_not_negative",
    )


def test_deleting_a_customer_removes_its_keys_and_their_rows(db: Session):
    key = make_key(db)
    db.add(KeySpend(key_id=key.id, period="2026-10"))
    db.flush()
    db.add_all(
        [
            Reservation(
                key_id=key.id,
                period="2026-10",
                amount_micros=1_000,
                expires_at=datetime.now(UTC),
            ),
            RequestLog(key_id=key.id, model="m", status="ok"),
            UsageOutbox(customer_id=key.customer_id, cost_micros=1, window_start=datetime.now(UTC)),
        ]
    )
    db.flush()

    db.execute(delete(Customer).where(Customer.id == key.customer_id))

    for model in (ApiKey, KeySpend, Reservation, RequestLog, UsageOutbox):
        assert db.scalars(select(model)).all() == []
