"""Canonical clock for the R/W Book Club — one source of "now"/"today" and meeting datetimes,
all in the club's LOCAL timezone (US Central by default, via CLUB_TIMEZONE).

The club operates in a single timezone, and meeting dates + times are stored LOCAL
(America/Chicago). So anything that compares against them — "is this meeting upcoming?", "how
many days until?", the predictive last-Tuesday schedule — must use the club's local day, not UTC
and not the process's implicit system timezone (which differs under launchd/CI). This module is
that single definition.

NOT for absolute-instant timestamps: event/audit/created_at logs stay UTC (see db._now) and must
not use this module — those are points in time, not club-local calendar facts.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent import config

# A meeting with no explicit start_time is assumed to start at this local hour — the club meets in
# the evening. Used for the cadence timing and the upcoming/past boundary so neither ever keys off
# midnight.
DEFAULT_MEETING_HOUR = 18
# A meeting stops being "upcoming" this long after it starts — roughly when it wraps up — so the
# current meeting rolls to "past" the evening it happens, not at local midnight the next day.
MEETING_ROLL_BUFFER = timedelta(hours=3)


def tz() -> ZoneInfo | timezone:
    """The club's timezone (America/Chicago), or UTC if the zoneinfo db is unavailable."""
    try:
        return ZoneInfo(config.CLUB_TIMEZONE)
    except ZoneInfoNotFoundError:
        return timezone.utc


def club_now() -> datetime:
    """The current instant as an aware datetime in the club's local timezone."""
    return datetime.now(tz())


def club_today() -> date:
    """Today's date in the club's local timezone."""
    return club_now().date()


def club_today_iso() -> str:
    return club_today().isoformat()


def _easter(year: int) -> date:
    """Easter Sunday (Gregorian) — the anonymous computus algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (a + 11 * h) // 319
    r = (2 * e + 2 * i - k - h + m + 32) % 7
    month = (h - m + r + 90) // 25
    day = (h - m + r + month + 19) % 32
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The nth <weekday> (Mon=0) of a month; n=-1 for the last one."""
    if n > 0:
        first = date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        return first + timedelta(days=offset + 7 * (n - 1))
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def us_holiday(d: date) -> str | None:
    """The greeting-worthy US holiday on `d`, or None. The set is the social canon the club
    would actually wish each other — not the full federal calendar."""
    fixed = {
        (1, 1): "New Year's Day",
        (2, 14): "Valentine's Day",
        (3, 17): "St. Patrick's Day",
        (6, 19): "Juneteenth",
        (7, 4): "Independence Day",
        (10, 31): "Halloween",
        (11, 11): "Veterans Day",
        (12, 24): "Christmas Eve",
        (12, 25): "Christmas Day",
        (12, 31): "New Year's Eve",
    }
    name = fixed.get((d.month, d.day))
    if name:
        return name
    if d == _easter(d.year):
        return "Easter"
    if d == _nth_weekday(d.year, 5, 0, -1):
        return "Memorial Day"
    if d == _nth_weekday(d.year, 9, 0, 1):
        return "Labor Day"
    if d == _nth_weekday(d.year, 11, 3, 4):
        return "Thanksgiving"
    return None


def _hh_mm(start_time: str | None) -> tuple[int, int]:
    if start_time:
        try:
            return int(str(start_time)[:2]), int(str(start_time)[3:5])
        except (ValueError, IndexError):
            pass
    return DEFAULT_MEETING_HOUR, 0


def meeting_start(meeting_date: str | None, start_time: str | None = None) -> datetime | None:
    """A meeting's local start as a club-tz aware datetime. Honors start_time ('HH:MM'), else the
    default evening hour. None if the date can't be parsed."""
    try:
        d = date.fromisoformat(str(meeting_date)[:10])
    except (ValueError, TypeError):
        return None
    hour, minute = _hh_mm(start_time)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz())


def meeting_end(meeting_date: str | None, start_time: str | None = None) -> datetime | None:
    """When a meeting stops being "upcoming" — its start plus the roll buffer. None if the date
    can't be parsed."""
    start = meeting_start(meeting_date, start_time)
    return start + MEETING_ROLL_BUFFER if start else None


def is_upcoming(meeting_date: str | None, start_time: str | None = None, *,
                now: datetime | None = None) -> bool:
    """True if the meeting has not yet passed — i.e. now is before start + buffer. This is the
    single definition of "upcoming vs past" for a meeting (there is no placeholder flag)."""
    end = meeting_end(meeting_date, start_time)
    return end is not None and (now or club_now()) < end
