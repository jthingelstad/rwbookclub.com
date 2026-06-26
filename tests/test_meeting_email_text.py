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
    assert meeting_rules.roll_call_subject(STATUS) == "Roll call: A World Appears on 2026-06-30"


def test_roll_call_subject_falls_back_without_book():
    status = {"meeting": {"date": "2026-06-30", "book": None}, "counts": STATUS["counts"]}
    assert meeting_rules.roll_call_subject(status) == "Roll call: the next meeting on 2026-06-30"


def test_roll_call_email_body_includes_meeting_picker_and_counts():
    body = meeting_rules.roll_call_email_body("Erik", STATUS)
    assert body.startswith("Hi Erik,")
    assert "A World Appears" in body
    assert "2026-06-30" in body
    assert "Jamie picked this one" in body
    assert "2 yes, 1 no, 0 unsure, 2 pending" in body
    assert "We need 3 yes responses." in body
    # No note → no trailing extra paragraph beyond the standard body.
    assert body.count("\n\n") == 4


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
