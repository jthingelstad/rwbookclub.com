"""Meeting roll-call rules."""

from __future__ import annotations

from datetime import date


def test_last_tuesday():
    from agent.club import meeting_rules

    assert meeting_rules.last_tuesday(2026, 6).isoformat() == "2026-06-30"
    assert meeting_rules.last_tuesday(2026, 5).isoformat() == "2026-05-26"


def test_next_last_tuesday_moves_to_next_month_after_date():
    from agent.club import meeting_rules

    assert meeting_rules.next_last_tuesday(date(2026, 5, 28)).isoformat() == "2026-06-30"
    assert meeting_rules.next_last_tuesday(date(2026, 6, 1)).isoformat() == "2026-06-30"


def test_next_meeting_knows_current_scheduled_book():
    from agent.club import meeting_rules

    meeting = meeting_rules.next_meeting()
    assert meeting["meetingKey"] == "a-world-appears"
    assert meeting["date"] == "2026-06-30"
    assert meeting["book"]["title"] == "A World Appears"
    assert meeting["book"]["authors"] == ["Michael Pollan"]


def test_meeting_status_flags_picker_conflict(fresh_db):
    from agent import clubdb, db
    from agent.club import meeting_rules

    meeting = meeting_rules.next_meeting()
    mid = meeting["meetingId"]
    db.upsert_roll_call(meeting_id=mid, channel_id="ch1")
    for slug in ("jamie", "tom", "nick"):
        db.set_attendance(meeting_id=mid, member_id=clubdb.lookup_member_id(slug), status="yes")
    for slug in meeting["pickerSlugs"]:
        db.set_attendance(meeting_id=mid, member_id=clubdb.lookup_member_id(slug), status="no")

    status = meeting_rules.meeting_status(mid)
    assert "picker_unavailable" in status["risks"]
    assert status["recommendation"] == "needs_attention"


def test_meeting_status_ready_when_quorum_and_picker(fresh_db):
    from agent import clubdb, db
    from agent.club import meeting_rules

    meeting = meeting_rules.next_meeting()
    mid = meeting["meetingId"]
    db.upsert_roll_call(meeting_id=mid, channel_id="ch1")
    yes = set(meeting["pickerSlugs"])
    yes.update(["jamie", "tom", "nick"])
    for slug in sorted(yes):
        db.set_attendance(meeting_id=mid, member_id=clubdb.lookup_member_id(slug), status="yes")

    status = meeting_rules.meeting_status(mid)
    assert status["hasQuorum"] is True
    assert status["pickerAvailable"] is True
    assert status["recommendation"] == "ready"
