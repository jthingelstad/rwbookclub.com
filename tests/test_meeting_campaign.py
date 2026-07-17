"""The autonomous meeting-prep cadence: the outreach_plan state machine + its rails.

outreach_plan is pure (today is injected), so these are deterministic. It decides, per member,
whether there's an open need (roll call until attendance answered, then reading until finished),
applies the email-only + 2-week-window + 3-day-floor gates, and flags mustReach (kickoff / ceiling /
final push). The discretionary REACH/WAIT call lives in oliver.decide_outreach (tested separately).
"""

from __future__ import annotations

from datetime import date, timedelta

from agent.club import meeting_campaign as mc

TODAY = date(2026, 6, 20)


def _member(
    slug,
    *,
    attendance="pending",
    reading="unknown",
    email=True,
    last_asked_days=None,
    attendance_asks=0,
    reading_asks=0,
    progress=None,
):
    last_asked = (
        None if last_asked_days is None else (TODAY - timedelta(days=last_asked_days)).isoformat()
    )
    return {
        "member": slug.title(),
        "memberSlug": slug,
        "memberId": 1,
        "attendance": attendance,
        "reading": reading,
        "readingProgress": progress,
        "emailLinked": email,
        "lastAskedAt": last_asked,
        "attendanceAsks": attendance_asks,
        "readingCheckinCount": reading_asks,
    }


def _plan(days, *members, today=TODAY):
    return mc.outreach_plan({"daysUntilMeeting": days, "members": list(members)}, today=today)


def _by_slug(plan):
    return {c["memberSlug"]: c for c in plan}


def test_attendance_needed_for_pending_and_unsure():
    got = _by_slug(
        _plan(
            10,
            _member("a", attendance="pending", last_asked_days=4),
            _member("b", attendance="unsure", last_asked_days=4),
        )
    )
    assert got["a"]["kind"] == "attendance"
    assert got["b"]["kind"] == "attendance"


def test_reading_needed_until_finished_only():
    got = _by_slug(
        _plan(
            10,
            _member("ontrack", attendance="yes", reading="on_track", last_asked_days=4),
            _member("behind", attendance="yes", reading="behind", last_asked_days=4),
            _member("done", attendance="yes", reading="finished", last_asked_days=4),
            _member("declined", attendance="no", last_asked_days=4),
        )
    )
    # on_track is NOT "done" for the cadence — it keeps getting check-ins until finished.
    assert got["ontrack"]["kind"] == "reading"
    assert got["behind"]["kind"] == "reading"
    assert "done" not in got  # finished → nothing left to collect
    assert "declined" not in got  # not attending → nothing


def test_email_only_skips_unlinked_members():
    assert _plan(10, _member("a", email=False, last_asked_days=4)) == []


def test_three_day_floor_blocks_recent_outreach():
    assert _plan(10, _member("a", last_asked_days=1)) == []  # asked yesterday → too soon
    assert _by_slug(_plan(10, _member("a", last_asked_days=3)))["a"]["kind"] == "attendance"


def test_first_contact_is_forced_kickoff():
    # Never asked and never answered → force the first contact so the conversation always starts.
    assert _by_slug(_plan(10, _member("a", last_asked_days=None)))["a"]["mustReach"] is True


def test_already_engaged_member_is_not_force_pinged():
    # Confirmed yes + reading on_track (responded), never asked via the tracked path → Oliver decides,
    # NOT a forced kickoff. This is the "don't pester someone already engaged" case.
    c = _by_slug(
        _plan(8, _member("a", attendance="yes", reading="on_track", last_asked_days=None))
    )["a"]
    assert c["kind"] == "reading" and c["mustReach"] is False


def test_give_up_on_a_silent_member():
    # Still pending after GIVE_UP_AFTER_ASKS roll-call asks and no answer → give up, don't pester.
    assert _plan(8, _member("a", attendance="pending", attendance_asks=3, last_asked_days=4)) == []
    # Fewer asks → still pursuing.
    assert (
        _by_slug(
            _plan(8, _member("a", attendance="pending", attendance_asks=2, last_asked_days=4))
        )["a"]["kind"]
        == "attendance"
    )


def test_give_up_only_applies_to_the_silent():
    # Reported reading once (on_track) but many asks → NOT given up; an engaged member keeps going.
    c = _by_slug(
        _plan(
            8, _member("a", attendance="yes", reading="on_track", reading_asks=5, last_asked_days=4)
        )
    )["a"]
    assert c["kind"] == "reading"


def test_outside_the_two_week_window():
    assert _plan(15, _member("a", last_asked_days=4)) == []  # > 14 days out
    assert _plan(-1, _member("a", last_asked_days=4)) == []  # meeting already passed


def test_no_reading_checkin_cap():
    # Many prior reading asks, still due — there's no 3-ask cap anymore; keep going until finished.
    c = _by_slug(
        _plan(
            8, _member("a", attendance="yes", reading="behind", last_asked_days=4, reading_asks=9)
        )
    )["a"]
    assert c["kind"] == "reading" and c["asksSoFar"] == 9 and c["mustReach"] is False


def test_asks_so_far_comes_from_the_right_counter():
    att = _by_slug(
        _plan(10, _member("a", attendance="pending", last_asked_days=4, attendance_asks=2))
    )["a"]
    assert att["asksSoFar"] == 2
    rd = _by_slug(
        _plan(
            10, _member("b", attendance="yes", reading="behind", last_asked_days=4, reading_asks=3)
        )
    )["b"]
    assert rd["asksSoFar"] == 3
