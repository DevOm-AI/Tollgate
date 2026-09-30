"""The headline guarantee, end to end: a burst can't push a key past its budget.

One run of each: loadtest/overspend.py runs it 10 times and prints the README table.
"""

import anyio
from sqlalchemy import Engine

from app.core.config import Settings
from loadtest.overspend import BUDGET_MICROS, COST_MICROS, run_once


def run(db_engine: Engine, mode: str):
    return anyio.run(
        lambda: run_once(
            db_engine.url.render_as_string(hide_password=False),
            Settings(_env_file=None).redis_url,
            mode=mode,
            requests=500,
            delay_ms=300,
        )
    )


def test_500_requests_at_once_never_overspend_a_1_dollar_budget(db_engine):
    result = run(db_engine, "reserve")

    assert result.succeeded == BUDGET_MICROS // COST_MICROS == 100
    assert result.refused == 400
    assert result.spent_micros == BUDGET_MICROS
    assert result.reserved_micros == 0
    assert result.holds


def test_check_then_add_would_overspend(db_engine):
    # What the conditional UPDATE prevents: every request passes the check before any
    # of them has added its cost.
    result = run(db_engine, "naive")

    assert result.succeeded > 100
    assert result.overspend_micros > 0
    assert not result.holds
