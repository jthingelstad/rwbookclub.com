"""Regression: meeting dates are now LOCAL 'YYYY-MM-DD' (naive). The scheduler computes
days-until against an offset-AWARE `now`, so _parse must make a bare date tz-aware or the
subtraction raises 'can't subtract offset-naive and offset-aware datetimes'."""
from datetime import datetime, timezone

from agent import scheduler


def test_parse_makes_local_date_timezone_aware():
    md = scheduler._parse("2026-06-30")           # local date, no time/zone
    assert md is not None and md.tzinfo is not None
    # arithmetic against an aware now must not raise
    days = (md - datetime.now(timezone.utc)).total_seconds() / 86400
    assert isinstance(days, float)
    # legacy 'Z' datetimes stay UTC-aware
    assert scheduler._parse("2026-06-30T23:30:00.000Z").tzinfo is not None


def test_due_notifications_runs_with_local_dates():
    # The live regression: this raised a TypeError before _parse attached a tz.
    out = scheduler.due_notifications(datetime.now(timezone.utc), set())
    assert isinstance(out, list)
