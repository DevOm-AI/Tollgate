import asyncio
import logging

import anyio
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.jobs import __main__ as jobs_main
from app.jobs import push_usage, reconcile_usage, reset_demo, sweep_reservations


@pytest.fixture
def started(monkeypatch) -> list[str]:
    """Replaces each job's loop with one that records it started and returns."""
    names: list[str] = []

    def recorder(name):
        async def run(*args):
            names.append(name)

        return run

    monkeypatch.setattr(sweep_reservations, "run_forever", recorder("sweep"))
    monkeypatch.setattr(push_usage, "run_forever", recorder("push_usage"))
    monkeypatch.setattr(reconcile_usage, "run_forever", recorder("reconcile"))
    monkeypatch.setattr(reset_demo, "run_forever", recorder("reset_demo"))
    return names


def use(monkeypatch, *, billing, demo_key):
    monkeypatch.setattr(jobs_main, "get_stripe_billing", lambda: billing)
    monkeypatch.setattr(
        jobs_main, "get_settings", lambda: Settings(_env_file=None, demo_api_key=demo_key)
    )


def test_every_job_runs_when_stripe_and_the_demo_are_set_up(monkeypatch, started):
    use(monkeypatch, billing=object(), demo_key=SecretStr("tg_live_demo"))

    anyio.run(jobs_main.main)

    assert sorted(started) == ["push_usage", "reconcile", "reset_demo", "sweep"]


def test_without_stripe_or_a_demo_only_the_sweep_runs(monkeypatch, started, caplog):
    use(monkeypatch, billing=None, demo_key=None)

    with caplog.at_level(logging.WARNING, logger="app.jobs"):
        anyio.run(jobs_main.main)

    assert started == ["sweep"]
    assert "usage stays in the outbox" in caplog.text


@pytest.mark.parametrize(
    ("module", "job_name", "args"),
    [
        (push_usage, "push_usage", (object(),)),
        (reconcile_usage, "reconcile", (object(),)),
        (reset_demo, "reset_demo_spend", ("tg_live_demo",)),
    ],
)
def test_job_loops_keep_going_after_a_failure(monkeypatch, caplog, module, job_name, args):
    calls = []

    async def flaky(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database away")
        raise asyncio.CancelledError  # Stop the loop, as a shutdown would.

    monkeypatch.setattr(module, job_name, flaky)

    async def no_wait(*_args):
        return None

    monkeypatch.setattr(module.anyio, "sleep", no_wait)
    if hasattr(module, "seconds_until_daily"):
        monkeypatch.setattr(module, "seconds_until_daily", lambda *a: 0)
    if hasattr(module, "seconds_until_next_run"):
        monkeypatch.setattr(module, "seconds_until_next_run", lambda *a: 0)

    with caplog.at_level(logging.ERROR), pytest.raises(asyncio.CancelledError):
        anyio.run(module.run_forever, *args)

    assert len(calls) == 2
    assert "failed" in caplog.text
