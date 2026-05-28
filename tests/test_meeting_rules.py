"""Meeting roll-call rules."""

from __future__ import annotations

from datetime import date


def test_last_tuesday():
    from agent import meeting_rules

    assert meeting_rules.last_tuesday(2026, 6).isoformat() == "2026-06-30"
    assert meeting_rules.last_tuesday(2026, 5).isoformat() == "2026-05-26"


def test_next_last_tuesday_moves_to_next_month_after_date():
    from agent import meeting_rules

    assert meeting_rules.next_last_tuesday(date(2026, 5, 28)).isoformat() == "2026-06-30"
    assert meeting_rules.next_last_tuesday(date(2026, 6, 1)).isoformat() == "2026-06-30"


def test_meeting_status_flags_picker_conflict(fresh_db):
    from agent import db, meeting_rules

    meeting = meeting_rules.next_meeting()
    db.upsert_roll_call(meeting_key=meeting["meetingKey"], channel_id="ch1")
    for slug in ("jamie", "tom", "nick"):
        db.set_attendance(meeting_key=meeting["meetingKey"], member_slug=slug, status="yes")
    for slug in meeting["pickerSlugs"]:
        db.set_attendance(meeting_key=meeting["meetingKey"], member_slug=slug, status="no")

    status = meeting_rules.meeting_status(meeting["meetingKey"])
    assert "picker_unavailable" in status["risks"]
    assert status["recommendation"] == "needs_attention"


def test_meeting_status_ready_when_quorum_and_picker(fresh_db):
    from agent import db, meeting_rules

    meeting = meeting_rules.next_meeting()
    db.upsert_roll_call(meeting_key=meeting["meetingKey"], channel_id="ch1")
    yes = set(meeting["pickerSlugs"])
    yes.update(["jamie", "tom", "nick"])
    for slug in sorted(yes):
        db.set_attendance(meeting_key=meeting["meetingKey"], member_slug=slug, status="yes")

    status = meeting_rules.meeting_status(meeting["meetingKey"])
    assert status["hasQuorum"] is True
    assert status["pickerAvailable"] is True
    assert status["recommendation"] == "ready"
