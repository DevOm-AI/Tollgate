"""The crash test: kill Tollgate mid-request and check that no money is lost or billed twice.

1. A real Tollgate server answers 100 requests (settled: spend, request rows, usage outbox).
2. 200 slow requests go in; once all 200 hold budget reservations, the server gets SIGKILL.
3. Tollgate restarts. Time is fast-forwarded in the database (reservations past their
   10-minute expiry, usage over a minute old) instead of waiting, then the real jobs run:
   the sweep releases the leaked reservations, and the outbox push sends usage to Stripe
   (a fake Stripe here, with Stripe's duplicate-identifier behaviour).
4. The push is replayed as if Tollgate crashed again after Stripe said OK but before the
   rows were marked sent.

It passes if spend equals exactly what the 100 answered requests cost, nothing stays held,
and Stripe ends up with that same amount, once.

    uv run python -m loadtest.crash
"""

import argparse
import contextlib
import os
import signal
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import anyio
import httpx2
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.billing.budget import current_period
from app.billing.stripe_billing import StripeBilling
from app.core.config import Settings
from app.core.security import generate_api_key
from app.jobs.push_usage import push_usage
from app.jobs.sweep_reservations import sweep
from app.models import ApiKey, Customer, KeySpend, RequestLog, Reservation, UsageOutbox
from loadtest.overhead import free_port
from loadtest.overspend import scratch_database
from tests.fake_stripe import FakeStripe

ROOT = Path(__file__).resolve().parents[1]
STRIPE_CUSTOMER = "cus_crash_test"


@dataclass
class CrashReport:
    answered_before_crash: int = 0
    killed_in_flight: int = 0
    open_reservations_after_crash: int = 0
    held_after_crash_micros: int = 0
    released_by_sweep: int = 0
    spent_micros: int = 0
    reserved_after_sweep_micros: int = 0
    cost_of_answered_micros: int = 0
    recorded_usage_micros: int = 0
    stripe_total_micros: int = 0
    stripe_total_after_replay_micros: int = 0
    stripe_events: int = 0
    stripe_sends: int = 0
    served_after_restart: bool = False

    @property
    def passed(self) -> bool:
        return (
            self.open_reservations_after_crash == self.killed_in_flight > 0
            and self.held_after_crash_micros > 0
            and self.released_by_sweep == self.killed_in_flight
            and self.reserved_after_sweep_micros == 0
            and self.spent_micros
            == self.cost_of_answered_micros
            == self.recorded_usage_micros
            == self.stripe_total_micros
            == self.stripe_total_after_replay_micros
            and self.served_after_restart
        )


@contextlib.contextmanager
def tollgate(port: int, env: dict[str, str]) -> Iterator[subprocess.Popen]:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--port",
            str(port),
            "--log-level",
            "critical",
            "--no-access-log",
        ],
        cwd=ROOT,
        env=os.environ | env,
    )
    deadline = time.monotonic() + 30
    while True:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/docs", timeout=1)
            break
        except OSError:
            if time.monotonic() > deadline or process.poll() is not None:
                process.kill()
                raise RuntimeError("Tollgate didn't start") from None
            time.sleep(0.2)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


async def fire(client: httpx2.AsyncClient, key: str, count: int) -> list[int | str]:
    """Send `count` requests at once; each result is a status code or the error's name."""
    results: list[int | str] = []

    async def one() -> None:
        try:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "mock", "messages": [{"role": "user", "content": "Hi"}]},
                headers={"Authorization": f"Bearer {key}"},
            )
            results.append(response.status_code)
        except httpx2.HTTPError as exc:
            results.append(type(exc).__name__)

    async with anyio.create_task_group() as group:
        for _ in range(count):
            group.start_soon(one)
    return results


async def run_crash_test(
    database_url: str, redis_url: str, *, answered: int = 100, in_flight: int = 200
) -> CrashReport:
    report = CrashReport()
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    new_key = generate_api_key()
    async with sessions() as db:
        customer = Customer(name="Crash test", stripe_customer_id=STRIPE_CUSTOMER)
        db.add(customer)
        await db.flush()
        key = ApiKey(
            customer_id=customer.id,
            key_hash=new_key.key_hash,
            prefix=new_key.prefix,
            rpm_limit=1_000_000,
            tpm_limit=2_000_000_000,
            monthly_budget_micros=100_000_000,
        )
        db.add(key)
        await db.commit()

    async def open_reservations() -> int:
        async with sessions() as db:
            return await db.scalar(
                select(func.count())
                .select_from(Reservation)
                .where(Reservation.key_id == key.id, Reservation.settled_at.is_(None))
            )

    port = free_port()
    env = {
        "DATABASE_URL": database_url,
        "REDIS_URL": redis_url,
        # Slow answers, so the in-flight requests are still waiting when the server dies.
        "MOCK_DELAY_MS": "4000",
    }
    limits = httpx2.Limits(max_connections=None, max_keepalive_connections=None)
    base_url = f"http://127.0.0.1:{port}"

    # 1-2. Answer some requests, then kill the server with more in flight.
    with tollgate(port, env) as server:
        async with httpx2.AsyncClient(base_url=base_url, limits=limits, timeout=60) as client:
            report.answered_before_crash = (await fire(client, new_key.key, answered)).count(200)

            async with anyio.create_task_group() as group:
                group.start_soon(fire, client, new_key.key, in_flight)
                with anyio.fail_after(60):
                    while await open_reservations() < in_flight:
                        await anyio.sleep(0.05)
                server.send_signal(signal.SIGKILL)  # No shutdown, no cleanup: a crash.
                server.wait()
    report.killed_in_flight = in_flight

    async with sessions() as db:
        spend = await db.get(KeySpend, (key.id, current_period()))
        report.held_after_crash_micros = spend.reserved_micros
    report.open_reservations_after_crash = await open_reservations()

    # 3. Restart, let "time pass", and run the real jobs.
    with tollgate(port, env | {"MOCK_DELAY_MS": "0"}):
        async with httpx2.AsyncClient(base_url=base_url, timeout=30) as client:
            report.served_after_restart = (await fire(client, new_key.key, 1)) == [200]
        report.answered_before_crash += 1  # The request after restart is billed too.

    async with sessions() as db:
        await db.execute(
            text(
                "UPDATE reservations SET expires_at = now() - interval '1 second' "
                "WHERE settled_at IS NULL AND key_id = :key_id"
            ),
            {"key_id": key.id},
        )
        await db.execute(
            text(
                "UPDATE usage_outbox SET window_start = window_start - interval '2 minutes' "
                "WHERE customer_id = :customer_id"
            ),
            {"customer_id": customer.id},
        )
        await db.commit()
    before = await open_reservations()
    await sweep(sessions)
    report.released_by_sweep = before - await open_reservations()

    stripe = FakeStripe()
    billing = StripeBilling(stripe)
    await push_usage(billing, sessions)
    await push_usage(billing, sessions)  # A second run finds nothing left to send.
    report.stripe_total_micros = _stripe_total(stripe)

    # 4. A crash after Stripe said OK, before the rows were marked sent: all of it again.
    async with sessions() as db:
        await db.execute(
            text("UPDATE usage_outbox SET sent_at = NULL WHERE customer_id = :customer_id"),
            {"customer_id": customer.id},
        )
        await db.commit()
    await push_usage(billing, sessions)
    report.stripe_total_after_replay_micros = _stripe_total(stripe)
    report.stripe_events = len(stripe.objects["meter_events"])
    report.stripe_sends = sum(
        1 for kind, action, *_ in stripe.calls if kind == "meter_events" and action == "create"
    )

    async with sessions() as db:
        spend = await db.get(KeySpend, (key.id, current_period()))
        report.spent_micros = spend.spent_micros
        report.reserved_after_sweep_micros = spend.reserved_micros
        report.cost_of_answered_micros = await db.scalar(
            select(func.coalesce(func.sum(RequestLog.cost_micros), 0)).where(
                RequestLog.key_id == key.id, RequestLog.status == "ok"
            )
        )
        report.recorded_usage_micros = await db.scalar(
            select(func.coalesce(func.sum(UsageOutbox.cost_micros), 0)).where(
                UsageOutbox.customer_id == customer.id
            )
        )
    await engine.dispose()
    return report


def _stripe_total(stripe: FakeStripe) -> int:
    return sum(int(event.payload["value"]) for event in stripe.objects["meter_events"])


def format_report(report: CrashReport) -> str:
    def d(micros: int) -> str:
        return f"${micros / 1_000_000:.6f}"

    rows = [
        ("Requests answered (100 before the crash, 1 after)", report.answered_before_crash),
        ("Requests in flight when the server was killed", report.killed_in_flight),
        ("Reservations left open by the crash", report.open_reservations_after_crash),
        ("Money held by them", d(report.held_after_crash_micros)),
        ("Reservations released by the sweep", report.released_by_sweep),
        ("Held after the sweep", d(report.reserved_after_sweep_micros)),
        ("Spent (key_spend)", d(report.spent_micros)),
        ("Cost of the answered requests (request log)", d(report.cost_of_answered_micros)),
        ("Usage recorded for Stripe (outbox)", d(report.recorded_usage_micros)),
        ("Usage in Stripe after the push", d(report.stripe_total_micros)),
        ("Usage in Stripe after replaying the push", d(report.stripe_total_after_replay_micros)),
        ("Meter events Stripe kept / sent", f"{report.stripe_events} / {report.stripe_sends}"),
        ("Served requests after restart", "yes" if report.served_after_restart else "no"),
    ]
    lines = ["| | |", "| --- | --- |", *(f"| {label} | {value} |" for label, value in rows)]
    return "\n".join(lines)


async def main_async(answered: int, in_flight: int) -> int:
    settings = Settings(_env_file=None)
    with scratch_database(settings.database_url) as database_url:
        report = await run_crash_test(
            database_url, settings.redis_url, answered=answered, in_flight=in_flight
        )
    print(format_report(report))
    print(f"\nNo money lost, nothing billed twice: {'yes' if report.passed else 'NO'}")
    return 0 if report.passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--answered", type=int, default=100)
    parser.add_argument("--in-flight", type=int, default=200)
    args = parser.parse_args()
    return anyio.run(main_async, args.answered, args.in_flight)


if __name__ == "__main__":
    raise SystemExit(main())
