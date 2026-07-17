"""Shared clock cases consumed by both Python and the website's Node tests."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from agent import clock

CASES = json.loads((Path(__file__).parent / "fixtures" / "clock_cases.json").read_text())


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_central_calendar_date_cases():
    for case in CASES["today"]:
        assert (
            _instant(case["now"]).astimezone(clock.tz()).date().isoformat() == case["centralDate"]
        )


def test_meeting_roll_boundary_cases():
    for case in CASES["upcoming"]:
        assert (
            clock.is_upcoming(case["meetingDate"], case["startTime"], now=_instant(case["now"]))
            is case["expected"]
        )
