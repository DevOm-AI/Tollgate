"""The overspend test: can a burst of requests push a key past its budget?

A key with a $1.00 budget; the mock provider priced so every request costs exactly $0.01;
500 requests fired at the same moment through the whole gateway (real Postgres, real Redis,
a pooled database connection like production). Then:

- at most 100 succeed, every other request gets 402,
- spent_micros <= 1,000,000 and nothing is left reserved.

It runs twice over: with Tollgate's reserve (one conditional UPDATE that claims the worst
case before the provider is called), and with a naive "check, then add" (read spent, compare
with the budget, add the cost after the answer). The naive version lives only here, swapped
in for the run; the app has no such mode.

    uv run python -m loadtest.overspend              # 10 runs of each, 500 requests
    uv run python -m loadtest.overspend --runs 3 --requests 200

Needs the compose Postgres and Redis (or DATABASE_URL / REDIS_URL). It works in a scratch
database it creates and drops.
"""

import argparse
import contextlib
import statistics
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import anyio
import httpx2
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis
from sqlalchemy import create_engine, make_url, pool, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import chat as chat_module
from app.billing.budget import Hold, Outcome, current_period
from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.core.redis import get_redis
from app.core.security import generate_api_key
from app.main import app
from app.models import ApiKey, Customer, KeySpend, ModelPrice
from app.providers.breaker import Breakers, get_breakers
from app.providers.catalog import Catalog, get_catalog
from app.providers.mock import MockProvider

BUDGET_MICROS = 1_000_000  # $1.00
COST_MICROS = 10_000  # $0.01 per request
OUTPUT_TOKENS = 10
# 10 output tokens at 1,000,000 micros per 1k = 10,000 micros; input is free. With
# max_tokens = 10 the worst case equals the real cost, so exactly 100 requests fit.
PRICE = {"input_micros_per_1k": 0, "output_micros_per_1k": 1_000_000}

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


class OverspendMock(MockProvider):
    """The mock under its own name, so its test price never touches the real mock's."""

    name = "overspend-mock"


MODEL = "overspend-mock/mock"


@dataclass
class RunResult:
    mode: str
    requests: int
    statuses: Counter[int] = field(default_factory=Counter)
    spent_micros: int = 0
    reserved_micros: int = 0
    budget_micros: int = BUDGET_MICROS

    @property
    def succeeded(self) -> int:
        return self.statuses[200]

    @property
    def refused(self) -> int:
        return self.statuses[402]

    @property
    def overspend_micros(self) -> int:
        return max(0, self.spent_micros - self.budget_micros)

    @property
    def holds(self) -> bool:
        """Tollgate's promise for this run."""
        return (
            self.succeeded <= self.budget_micros // COST_MICROS
            and self.spent_micros <= self.budget_micros
            and self.succeeded + self.refused == self.requests
            and self.reserved_micros == 0
        )


# --- The naive budget check, for comparison only ---


async def naive_reserve(db: AsyncSession, key: ApiKey, amount_micros: int, now=None) -> Hold | None:
    """Check, then add: read what's spent, compare with the budget, claim nothing."""
    period = current_period(now)
    await db.execute(insert(KeySpend).values(key_id=key.id, period=period).on_conflict_do_nothing())
    spent = await db.scalar(
        select(KeySpend.spent_micros).where(KeySpend.key_id == key.id, KeySpend.period == period)
    )
    await db.commit()
    if spent + amount_micros > key.monthly_budget_micros:
        return None
    return Hold(uuid.uuid4(), key.id, key.customer_id, period, amount_micros)


async def naive_settle(db: AsyncSession, hold: Hold, cost_micros: int, outcome: Outcome) -> int:
    """...and add the cost once the answer is in."""
    await db.execute(
        update(KeySpend)
        .where(KeySpend.key_id == hold.key_id, KeySpend.period == hold.period)
        .values(spent_micros=KeySpend.spent_micros + cost_micros)
    )
    await db.commit()
    return cost_micros


@contextlib.contextmanager
def naive_budget() -> Iterator[None]:
    original = chat_module.reserve, chat_module.settle
    chat_module.reserve, chat_module.settle = naive_reserve, naive_settle
    try:
        yield
    finally:
        chat_module.reserve, chat_module.settle = original


# --- One run ---


async def run_once(
    database_url: str,
    redis_url: str,
    *,
    mode: str = "reserve",
    requests: int = 500,
    delay_ms: int = 500,
) -> RunResult:
    """Fire `requests` requests at once at a fresh $1.00 key and report what happened."""
    engine = create_async_engine(database_url, pool_size=20, max_overflow=20, pool_timeout=120)
    sessions = async_sessionmaker(engine, autoflush=False, expire_on_commit=False)
    redis = Redis.from_url(redis_url)
    new_key = generate_api_key()
    async with sessions() as db:
        await db.execute(
            insert(ModelPrice)
            .values(provider=OverspendMock.name, model="mock", **PRICE)
            .on_conflict_do_update(index_elements=["provider", "model"], set_=PRICE)
        )
        customer = Customer(name="Overspend test")
        db.add(customer)
        await db.flush()
        key = ApiKey(
            customer_id=customer.id,
            key_hash=new_key.key_hash,
            prefix=new_key.prefix,
            rpm_limit=1_000_000,
            tpm_limit=2_000_000_000,
            monthly_budget_micros=BUDGET_MICROS,
        )
        db.add(key)
        await db.commit()

    async def get_test_db() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    # A slow answer keeps every request in flight together: the worst case for a budget.
    mock = OverspendMock(delay_ms=delay_ms, output_tokens=OUTPUT_TOKENS)
    breakers = Breakers()
    overrides = {
        get_db: get_test_db,
        get_redis: lambda: redis,
        get_catalog: lambda: Catalog([mock]),
        get_breakers: lambda: breakers,
        get_settings: lambda: Settings(_env_file=None),
    }
    saved = dict(app.dependency_overrides)
    app.dependency_overrides.update(overrides)
    result = RunResult(mode=mode, requests=requests)
    try:
        with naive_budget() if mode == "naive" else contextlib.nullcontext():
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(
                transport=transport, base_url="http://tollgate", timeout=300
            ) as client:
                start = anyio.Event()
                statuses: list[int] = []

                async def send() -> None:
                    await start.wait()  # All at the same moment.
                    response = await client.post(
                        "/v1/chat/completions",
                        json={
                            "model": MODEL,
                            "messages": [{"role": "user", "content": "Hi"}],
                            "max_tokens": OUTPUT_TOKENS,
                        },
                        headers={"Authorization": f"Bearer {new_key.key}"},
                    )
                    statuses.append(response.status_code)

                async with anyio.create_task_group() as group:
                    for _ in range(requests):
                        group.start_soon(send)
                    await anyio.sleep(0.1)
                    start.set()
                result.statuses = Counter(statuses)

        async with sessions() as db:
            spend = await db.get(KeySpend, (key.id, current_period()))
            result.spent_micros = spend.spent_micros if spend else 0
            result.reserved_micros = spend.reserved_micros if spend else 0
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)
        await redis.aclose()
        await engine.dispose()
    return result


# --- The report ---


@contextlib.contextmanager
def scratch_database(server_url: str) -> Iterator[str]:
    """A migrated database that's dropped afterwards."""
    url = make_url(server_url)
    name = f"tollgate_overspend_{uuid.uuid4().hex[:12]}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT", poolclass=pool.NullPool)
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    scratch = url.set(database=name)
    try:
        engine = create_engine(scratch, poolclass=pool.NullPool)
        with engine.begin() as connection:
            config = Config(ALEMBIC_INI)
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        engine.dispose()
        yield scratch.render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def dollars(micros: int) -> str:
    return f"${micros / 1_000_000:,.2f}"


def table(results: list[RunResult]) -> str:
    lines = [
        "| Run | Succeeded | 402 | Other | Spent | Overspend | Left reserved |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, r in enumerate(results, 1):
        other = r.requests - r.succeeded - r.refused
        lines.append(
            f"| {i} | {r.succeeded} | {r.refused} | {other} | {dollars(r.spent_micros)} | "
            f"{dollars(r.overspend_micros)} | {dollars(r.reserved_micros)} |"
        )
    return "\n".join(lines)


def summary(results: list[RunResult]) -> str:
    worst = max(r.overspend_micros for r in results)
    mean = statistics.mean(r.overspend_micros for r in results)
    return (
        f"{len(results)} runs of {results[0].requests} requests on a {dollars(BUDGET_MICROS)} "
        f"budget: worst overspend {dollars(worst)}, average {dollars(round(mean))}"
    )


async def main_async(runs: int, requests: int, delay_ms: int) -> int:
    settings = Settings(_env_file=None)
    with scratch_database(settings.database_url) as database_url:
        report = {}
        for mode in ("reserve", "naive"):
            results = []
            for _ in range(runs):
                results.append(
                    await run_once(
                        database_url,
                        settings.redis_url,
                        mode=mode,
                        requests=requests,
                        delay_ms=delay_ms,
                    )
                )
            report[mode] = results
    title = {
        "reserve": "Tollgate: reserve the worst case with one conditional UPDATE",
        "naive": 'Naive: "check, then add"',
    }
    for mode, results in report.items():
        print(f"\n### {title[mode]}\n\n{summary(results)}\n\n{table(results)}")
    held = all(r.holds for r in report["reserve"])
    print(f"\nTollgate's guarantee held in every run: {'yes' if held else 'NO'}")
    return 0 if held else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--requests", type=int, default=500)
    parser.add_argument("--delay-ms", type=int, default=500, help="mock provider answer time")
    args = parser.parse_args()
    return anyio.run(main_async, args.runs, args.requests, args.delay_ms)


if __name__ == "__main__":
    raise SystemExit(main())
