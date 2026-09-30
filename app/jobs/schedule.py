from datetime import datetime, time, timedelta


def seconds_until_daily(at: time, now: datetime) -> float:
    """Seconds from `now` until the next time of day `at` (both timezone-aware)."""
    next_run = datetime.combine(now.date(), at)
    if next_run <= now:
        next_run += timedelta(days=1)
    return (next_run - now).total_seconds()
