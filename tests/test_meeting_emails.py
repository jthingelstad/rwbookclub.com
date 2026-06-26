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
    assert "2026-06-30" in prompt
    assert "Jamie" in prompt
    assert "two days before" in prompt
    assert "reading history" in prompt


def test_topic_email_builds_subject_and_body(monkeypatch):
    monkeypatch.setattr(meeting_emails.oliver, "generate", lambda prompt: "TOPIC BODY")
    out = meeting_emails.topic_email(MEETING)
    assert out["body"] == "TOPIC BODY"  # signature is added later by outbound.send
    assert "A World Appears" in out["subject"]
    assert "2026-06-30" in out["subject"]


def test_week_reminder_passes_committed_names_to_compose(monkeypatch):
    captured = {}

    def fake_compose(kind, facts, **kwargs):
        captured.update(facts)
        return "WEEK BODY"

    monkeypatch.setattr(meeting_emails.oliver, "compose", fake_compose)
    status = {
        "attendance": [
            {"member": "Erik", "status": "yes"},
            {"member": "Loren", "status": "yes"},
            {"member": "Tom", "status": "pending"},
        ],
        "counts": {},
    }
    out = meeting_emails.week_reminder(MEETING, status)
    assert out["body"] == "WEEK BODY"
    assert "A World Appears" in out["subject"]
    assert captured["committed to attend so far"] == "Erik, Loren"  # only the yes responses
