"""Oliver's clock: the per-turn [Now: …] priming line and the holiday calendar.

The conftest freezes club_now at 2026-06-29 12:00 club time; the fixture world's upcoming
meeting is 2026-06-30 18:30 (A World Appears).
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from agent import clock, oliver


def _freeze(monkeypatch, y, m, d, hh=12, mm=0):
    frozen = datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("America/Chicago"))
    monkeypatch.setattr(clock, "club_now", lambda: frozen)


def test_now_line_date_time_and_tomorrow_meeting():
    line = oliver._now_line()
    assert line.startswith("[Now: Monday, June 29, 2026, 12:00 PM in Minneapolis.")
    assert "Next meeting:" in line and "tomorrow" in line
    assert line.endswith("]")


def test_now_line_meeting_today(monkeypatch):
    _freeze(monkeypatch, 2026, 6, 30, 9)
    line = oliver._now_line()
    assert "TODAY" in line


def test_now_line_counts_days(monkeypatch):
    _freeze(monkeypatch, 2026, 6, 20)
    assert "10 days away" in oliver._now_line()


def test_now_line_holiday_today_and_eve(monkeypatch):
    _freeze(monkeypatch, 2026, 7, 4)
    assert "Today is Independence Day." in oliver._now_line()
    _freeze(monkeypatch, 2026, 7, 3)
    assert "Tomorrow is Independence Day." in oliver._now_line()
    _freeze(monkeypatch, 2026, 7, 10)
    line = oliver._now_line()
    assert "Independence" not in line and "Today is" not in line


def test_question_block_leads_with_now():
    block = oliver._question_block("what day is it?", "Jamie", "jamie", None)
    assert block.startswith("[Now: ")
    assert "what day is it?" in block


def test_us_holiday_calendar():
    assert clock.us_holiday(date(2026, 7, 4)) == "Independence Day"
    assert clock.us_holiday(date(2026, 12, 25)) == "Christmas Day"
    assert clock.us_holiday(date(2026, 11, 26)) == "Thanksgiving"     # 4th Thursday
    assert clock.us_holiday(date(2026, 5, 25)) == "Memorial Day"      # last Monday
    assert clock.us_holiday(date(2026, 9, 7)) == "Labor Day"          # 1st Monday
    assert clock.us_holiday(date(2026, 4, 5)) == "Easter"             # computus
    assert clock.us_holiday(date(2025, 4, 20)) == "Easter"
    assert clock.us_holiday(date(2026, 7, 3)) is None
    assert clock.us_holiday(date(2026, 11, 19)) is None               # a plain Thursday


def test_doctrine_mentions_the_clock():
    assert "[Now:" in oliver.OPERATIONAL_PROMPT
    assert "never force a holiday greeting" in oliver.OPERATIONAL_PROMPT
