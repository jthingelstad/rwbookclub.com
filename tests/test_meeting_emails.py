"""The 2-day topic email and 1-week reminder builders."""
from agent.club import meeting_emails

MEETING = {
    "meetingKey": "a-world-appears",
    "date": "2026-06-30",
    "book": {"title": "A World Appears", "authors": ["Michael Pollan"]},
    "pickerNames": ["Jamie"],
}


def test_topic_email_prompt_includes_facts():
    prompt = meeting_emails.topic_email_prompt(MEETING)
    assert "A World Appears" in prompt
    assert "June 30" in prompt          # friendly date, not the ISO form
    assert "2026-06-30" not in prompt
    assert "Jamie" in prompt
    assert "two days before" in prompt
    assert "reading history" in prompt


def test_friendly_date():
    assert meeting_emails._friendly_date("2026-06-30") == "Tuesday, June 30"
    assert meeting_emails._friendly_date("2026-06-30T00:00:00Z") == "Tuesday, June 30"
    assert meeting_emails._friendly_date("not-a-date") == "not-a-date"


def test_extract_email_handles_unclosed_tag():
    # A truncated generation may open <email> but never close it.
    raw = "preamble\n\n<email>Hello all,\n\n## On the Book\n\n1. A question"
    out = meeting_emails._extract_email(raw)
    assert out.startswith("Hello all,")
    assert "<email>" not in out
    assert "preamble" not in out


def test_extract_email_strips_preamble_and_trailing_notes():
    raw = ("Good — I have what I need. Let me write this.\n\n"
           "<email>Hello all,\n\n## Connections\nstuff</email>\n\nnotes after")
    out = meeting_emails._extract_email(raw)
    assert out.startswith("Hello all,")
    assert "Good — I have" not in out
    assert "notes after" not in out


def test_extract_email_without_tags_returns_text():
    assert meeting_emails._extract_email("just an email") == "just an email"


def test_topic_email_builds_subject_and_body(monkeypatch):
    monkeypatch.setattr(meeting_emails.oliver, "generate", lambda prompt: "TOPIC BODY")
    out = meeting_emails.topic_email(MEETING)
    assert out["body"] == "TOPIC BODY"  # signature is added later by outbound.send
    assert "A World Appears" in out["subject"]
    assert "June 30" in out["subject"]  # friendly date, not ISO


def test_week_reminder_separates_yes_no_and_pending(monkeypatch):
    """A member who declined must be reported as 'not coming', never nudged as pending."""
    captured = {}

    def fake_compose(kind, facts, **kwargs):
        captured.update(facts)
        return "WEEK BODY"

    monkeypatch.setattr(meeting_emails.oliver, "compose", fake_compose)
    status = {
        "attendance": [
            {"member": "Erik", "status": "yes"},
            {"member": "Loren", "status": "yes"},
            {"member": "Tom", "status": "no"},       # clearly declined
            {"member": "Nick", "status": "pending"},
        ],
        "counts": {},
    }
    out = meeting_emails.week_reminder(MEETING, status)
    assert out["body"] == "WEEK BODY"
    assert "A World Appears" in out["subject"]
    assert captured["confirmed coming"] == "Erik, Loren"
    assert captured["not able to make it"] == "Tom"            # Tom is a 'no', not pending
    assert captured["still waiting to hear from"] == "Nick"    # only Nick gets nudged
