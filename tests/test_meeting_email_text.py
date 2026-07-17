"""The roll-call email text builders shared by the command path and the tool path.

These moved out of commands.py/tools.py into meeting_rules so the two senders can't
drift; this pins the subject/body wording and the `note` behavior.
"""

from agent.club import meeting_rules

STATUS = {
    "meeting": {
        "date": "2026-06-30",
        "book": {"title": "A World Appears"},
        "pickerNames": ["Jamie"],
    },
    "counts": {"yes": 2, "no": 1, "unsure": 0, "pending": 2, "quorumRequired": 3},
}


def test_roll_call_subject():
    assert (
        meeting_rules.roll_call_subject(STATUS) == "Roll call: A World Appears on Tuesday, June 30"
    )


def test_roll_call_subject_falls_back_without_book():
    status = {"meeting": {"date": "2026-06-30", "book": None}, "counts": STATUS["counts"]}
    assert (
        meeting_rules.roll_call_subject(status) == "Roll call: the next meeting on Tuesday, June 30"
    )


def test_roll_call_email_body_includes_meeting_picker_and_counts():
    body = meeting_rules.roll_call_email_body("Erik", STATUS)
    assert body.startswith("Hi Erik,")
    assert "A World Appears" in body
    assert "Tuesday, June 30" in body  # friendly date, not the ISO form
    assert "Jamie picked this one" in body
    assert "2 yes, 1 no, 0 unsure, 2 pending" in body
    assert "We need 3 yes responses." in body
    # No note → no trailing extra paragraph beyond the standard body.
    assert body.count("\n\n") == 4


def test_roll_call_email_body_includes_time_and_location_when_set():
    status = {
        "meeting": {
            "date": "2026-07-28",
            "startTime": "18:30",
            "location": "Broder's",
            "book": {"title": "Stiff"},
            "pickerNames": ["Tom"],
        },
        "counts": STATUS["counts"],
    }
    body = meeting_rules.roll_call_email_body("Erik", status)
    assert "Tuesday, July 28 at 6:30 PM" in body  # date + time
    assert "at Broder's" in body  # location when set


def test_roll_call_email_body_appends_optional_note():
    body = meeting_rules.roll_call_email_body("Erik", STATUS, note="Bring your own chair.")
    assert "Bring your own chair." in body
    # The note path is the only difference vs. the no-note body.
    assert "Bring your own chair." not in meeting_rules.roll_call_email_body("Erik", STATUS)


def test_days_until_text():
    assert meeting_rules.days_until_text("not-a-date") == ""
    # Relative phrasing is exercised; exact day depends on today, so just assert shape.
    out = meeting_rules.days_until_text("2099-01-01")
    assert out.startswith("in ") and out.endswith(" days")


def test_reading_checkin_email_body_shared_shape():
    # The command path and the request_reading_update tool both call this one helper.
    meeting = {
        "date": "2026-07-28",
        "startTime": "18:30",
        "location": "Broder's",
        "book": {"title": "Stiff"},
    }
    body = meeting_rules.reading_checkin_email_body("Erik", meeting)
    assert body.startswith("Hi Erik,")
    assert "Quick reading check-in for Stiff" in body
    assert "Tuesday, July 28 at 6:30 PM" in body  # friendly date + time, not the ISO form
    assert "at Broder's" in body  # location when set
    assert "finished" in body  # the reply-guidance line


def test_reading_checkin_email_body_appends_note():
    meeting = {"date": "2026-07-28", "book": {"title": "Stiff"}}
    body = meeting_rules.reading_checkin_email_body("Erik", meeting, note="Bring snacks.")
    assert "Bring snacks." in body
    assert "Bring snacks." not in meeting_rules.reading_checkin_email_body("Erik", meeting)
