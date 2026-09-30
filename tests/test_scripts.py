import anyio
from sqlalchemy import Engine, pool, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.core.security import hash_api_key
from app.models import ApiKey, Customer
from scripts import create_demo_key, setup_stripe
from tests.fake_stripe import FakeStripe


def test_demo_key_gets_ten_cents_and_five_requests_a_minute(db_engine: Engine, monkeypatch):
    async def run() -> str:
        engine = create_async_engine(db_engine.url, poolclass=pool.NullPool)
        monkeypatch.setattr(create_demo_key, "SessionLocal", async_sessionmaker(engine))
        monkeypatch.setattr(create_demo_key, "engine", engine)
        return await create_demo_key.create_demo_key()

    key = anyio.run(run)

    with Session(db_engine) as session:
        row = session.scalars(select(ApiKey).where(ApiKey.key_hash == hash_api_key(key))).one()
        customer = session.get(Customer, row.customer_id)
    assert key.startswith("tg_live_")
    assert (row.monthly_budget_micros, row.rpm_limit) == (100_000, 5)
    assert customer.name == "Tollgate demo"


def test_demo_key_script_prints_the_setting(monkeypatch, capsys):
    async def fake_create() -> str:
        return "tg_live_example"

    monkeypatch.setattr(create_demo_key, "create_demo_key", fake_create)

    assert create_demo_key.main() == 0
    assert "DEMO_API_KEY=tg_live_example" in capsys.readouterr().out


def test_stripe_setup_prints_the_meter_and_price(monkeypatch, capsys):
    from app.billing.stripe_billing import StripeBilling

    monkeypatch.setattr(setup_stripe, "get_stripe_billing", lambda: StripeBilling(FakeStripe()))

    assert setup_stripe.main() == 0
    out = capsys.readouterr().out
    assert "meter  mtr_" in out
    assert "price  price_" in out


def test_stripe_setup_without_a_key_fails_with_a_hint(monkeypatch, capsys):
    monkeypatch.setattr(setup_stripe, "get_stripe_billing", lambda: None)

    assert setup_stripe.main() == 1
    assert "STRIPE_SECRET_KEY is not set" in capsys.readouterr().err
