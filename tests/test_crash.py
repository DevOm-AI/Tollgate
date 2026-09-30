"""Kill the server mid-request: no money lost, nothing billed twice (a smaller round than
loadtest/crash.py, which the README's numbers come from)."""

import anyio
from sqlalchemy import Engine

from app.core.config import Settings
from loadtest.crash import run_crash_test


def test_crash_mid_request_loses_no_money_and_bills_nothing_twice(db_engine: Engine):
    report = anyio.run(
        lambda: run_crash_test(
            db_engine.url.render_as_string(hide_password=False),
            Settings(_env_file=None).redis_url,
            answered=20,
            in_flight=50,
        )
    )

    assert report.open_reservations_after_crash == 50
    assert report.released_by_sweep == 50
    assert report.reserved_after_sweep_micros == 0
    assert report.spent_micros == report.cost_of_answered_micros > 0
    assert report.recorded_usage_micros == report.spent_micros
    assert report.stripe_total_micros == report.stripe_total_after_replay_micros
    assert report.stripe_total_micros == report.spent_micros
    assert report.served_after_restart
    assert report.passed
