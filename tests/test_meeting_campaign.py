"""Meeting campaign timing and cap rules."""

from __future__ import annotations

from datetime import date


def _campaign(days: int, *, count: int = 0, last_contact: str | None = None) -> dict:
    member = {
        "member": "Jamie",
        "memberSlug": "jamie",
        "attendance": "yes",
        "reading": "unknown",
        "readingOk": False,
        "readingCheckinCount": count,
        "readingLastAskedAt": last_contact,
    }
    return {
        "daysUntilMeeting": days,
        "needsReading": [member],
    }


def test_first_reading_checkin_due_at_14_days():
    from agent.club import meeting_campaign

    assert meeting_campaign.reading_checkin_candidates(
        _campaign(14),
        today=date(2026, 6, 16),
    )[0]["checkinNumber"] == 1


def test_second_reading_checkin_waits_until_7_day_window_and_spacing():
    from agent.club import meeting_campaign

    assert meeting_campaign.reading_checkin_candidates(
        _campaign(8, count=1, last_contact="2026-06-16 10:00:00"),
        today=date(2026, 6, 22),
    ) == []
    assert meeting_campaign.reading_checkin_candidates(
        _campaign(7, count=1, last_contact="2026-06-20 10:00:00"),
        today=date(2026, 6, 23),
    )[0]["checkinNumber"] == 2
    assert meeting_campaign.reading_checkin_candidates(
        _campaign(7, count=1, last_contact="2026-06-22 10:00:00"),
        today=date(2026, 6, 23),
    ) == []


def test_third_reading_checkin_waits_until_2_day_window():
    from agent.club import meeting_campaign

    assert meeting_campaign.reading_checkin_candidates(
        _campaign(3, count=2, last_contact="2026-06-24 10:00:00"),
        today=date(2026, 6, 27),
    ) == []
    assert meeting_campaign.reading_checkin_candidates(
        _campaign(2, count=2, last_contact="2026-06-24 10:00:00"),
        today=date(2026, 6, 28),
    )[0]["checkinNumber"] == 3


def test_reading_checkins_stop_after_three():
    from agent.club import meeting_campaign

    assert meeting_campaign.reading_checkin_candidates(
        _campaign(1, count=3, last_contact="2026-06-27 10:00:00"),
        today=date(2026, 6, 29),
    ) == []
