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


def test_bookless_scheduled_meeting_remains_canonical(fresh_db):
    from agent import clubdb, context, corpus_gen, corpus_read, db
    from agent.club import meeting_campaign, meeting_rules

    meeting_id = clubdb.meeting_id_for_book_slug("a-world-appears")
    with db.connect() as conn:
        conn.execute("DELETE FROM club_meeting_books WHERE meeting_id = ?", (meeting_id,))
    corpus_gen.generate()

    upcoming = corpus_read.upcoming_meetings()
    assert upcoming[0] == {
        "meetingId": meeting_id,
        "meetingDate": "2026-06-30",
        "startTime": "18:30",
        "location": "Broder’s",
        "notes": None,
        "type": ["Book"],
        "hostSlugs": ["jamie"],
        "hostNames": ["Jamie"],
        "books": [],
        "slug": None,
        "title": None,
        "authors": [],
        "pickedBy": "Jamie",
        "topic": None,
    }

    meeting = meeting_rules.next_meeting()
    assert meeting["meetingId"] == meeting_id
    assert meeting["date"] == "2026-06-30"
    assert meeting["startTime"] == "18:30"
    assert meeting["book"] is None
    assert meeting["pickerSlugs"] == ["jamie"]

    horizon = meeting_rules.horizon()
    assert horizon["slots"][0]["meetingId"] == meeting_id
    assert horizon["slots"][0]["meetingDate"] == "2026-06-30"
    assert horizon["slots"][0]["status"] == "scheduled"
    assert horizon["slots"][0]["dateStatus"] == "scheduled"
    assert horizon["slots"][0]["bookStatus"] == "open"
    assert horizon["slots"][0]["book"] is None
    assert horizon["slots"][0]["picker"] == {"slug": "jamie", "name": "Jamie"}
    assert horizon["scheduledCount"] == len(upcoming)
    assert horizon["emptyCount"] == 5 - len(upcoming)

    status = meeting_rules.meeting_status()
    assert "book_not_picked" in status["risks"]
    assert status["recommendation"] != "ready"
    campaign = meeting_campaign.snapshot()
    assert campaign["needsReading"] == []
    assert any(action["kind"] == "book_pick" for action in campaign["recommendedActions"])
    compact_context = context.club_context()
    assert "Book not picked" in compact_context
    assert "hosted by Jamie" in compact_context


def test_horizon_current_fixture_matches_canonical_upcoming_books():
    from agent import corpus_read
    from agent.club import meeting_rules

    result = meeting_rules.horizon()
    upcoming = corpus_read.upcoming_meetings()
    assert result["depth"] == 5
    assert result["scheduledCount"] == len(upcoming)
    assert result["emptyCount"] == 5 - len(upcoming)
    assert result["slots"][0]["book"]["title"] == "A World Appears"


def _horizon_world(monkeypatch, *, upcoming_count=1, include_loren=True):
    from agent.club import meeting_rules

    members = [
        {"slug": "erik", "name": "Erik"},
        {"slug": "jamie", "name": "Jamie"},
        {"slug": "loren", "name": "Loren"},
        {"slug": "nick", "name": "Nick"},
        {"slug": "tom", "name": "Tom"},
    ]
    if not include_loren:
        members = [member for member in members if member["slug"] != "loren"]
    recency = {
        "loren": "2022-01-01",
        "nick": "2023-01-01",
        "erik": "2024-01-01",
        "tom": "2025-01-01",
        "jamie": "2026-01-01",
    }
    history = [
        {"slug": f"old-{slug}", "meetingDate": when, "pickerSlugs": [slug]}
        for slug, when in recency.items()
        if any(m["slug"] == slug for m in members)
    ]
    scheduled_pickers = ["jamie", "loren", "nick", "erik", "tom"][:upcoming_count]
    upcoming = [
        {
            "slug": f"future-{index}",
            "title": f"Future {index}",
            "authors": ["Author"],
            "meetingDate": f"2026-{index + 6:02d}-30",
            "placeholder": index == 1,
        }
        for index in range(upcoming_count)
    ]
    future_books = {
        row["slug"]: {**row, "pickerSlugs": [scheduled_pickers[index]]}
        for index, row in enumerate(upcoming)
    }
    monkeypatch.setattr(meeting_rules, "_current_members", lambda: members)
    monkeypatch.setattr(
        meeting_rules.corpus_read, "books", lambda: history + list(future_books.values())
    )
    monkeypatch.setattr(meeting_rules.corpus_read, "upcoming_meetings", lambda: upcoming)
    monkeypatch.setattr(
        meeting_rules.corpus_read, "find_book", lambda value: future_books.get(value)
    )
    return meeting_rules


def test_horizon_full_and_soft_scheduled_slots(monkeypatch):
    rules = _horizon_world(monkeypatch, upcoming_count=5)
    result = rules.horizon()
    assert result["scheduledCount"] == 5 and result["emptyCount"] == 0
    assert result["firstEmptyPicker"] is None
    assert result["slots"][1]["dateStatus"] == "soft"


def test_horizon_thin_runway_uses_loren_as_first_open_picker(monkeypatch):
    rules = _horizon_world(monkeypatch, upcoming_count=1)
    result = rules.horizon()
    assert result["scheduledCount"] == 1 and result["emptyCount"] == 4
    assert result["firstEmptyPicker"] == {"slug": "loren", "name": "Loren"}


def test_horizon_ignores_bookless_non_book_meetings(monkeypatch):
    rules = _horizon_world(monkeypatch, upcoming_count=1)
    scheduled_book = rules.corpus_read.upcoming_meetings()[0]
    non_book_meetings = [
        {
            "meetingId": 9001,
            "meetingDate": "2026-07-30",
            "type": ["Social"],
            "books": [],
            "slug": None,
            "title": None,
            "hostSlugs": ["loren"],
        },
        {
            "meetingId": 9002,
            "meetingDate": "2026-08-30",
            "type": ["Picking"],
            "books": [],
            "slug": None,
            "title": None,
            "hostSlugs": ["nick"],
        },
    ]
    open_book_meeting = {
        "meetingId": 9003,
        "meetingDate": "2026-09-30",
        "type": ["Book"],
        "books": [],
        "slug": None,
        "title": None,
        "hostSlugs": ["tom"],
    }
    monkeypatch.setattr(
        rules.corpus_read,
        "upcoming_meetings",
        lambda: [scheduled_book, *non_book_meetings, open_book_meeting],
    )

    result = rules.horizon()

    assert result["scheduledCount"] == 2 and result["emptyCount"] == 3
    assert [slot["meetingId"] for slot in result["slots"][:2]] == [None, 9003]
    assert result["slots"][1]["bookStatus"] == "open"
    assert result["slots"][1]["picker"] == {"slug": "tom", "name": "Tom"}
    assert result["firstEmptyPicker"] == {"slug": "loren", "name": "Loren"}


def test_horizon_empty_and_membership_change(monkeypatch):
    rules = _horizon_world(monkeypatch, upcoming_count=0, include_loren=False)
    result = rules.horizon(depth=4)
    assert result["scheduledCount"] == 0 and result["emptyCount"] == 4
    assert [slot["picker"]["slug"] for slot in result["slots"]] == ["nick", "erik", "tom", "jamie"]


def test_horizon_depth_clamps_and_cycles_current_members(monkeypatch):
    rules = _horizon_world(monkeypatch, upcoming_count=0)
    assert rules.horizon(depth=0)["depth"] == 1
    deep = rules.horizon(depth=99)
    assert deep["depth"] == 8 and len(deep["slots"]) == 8


def test_meeting_status_flags_picker_conflict(fresh_db):
    from agent import clubdb, db
    from agent.club import meeting_rules

    meeting = meeting_rules.next_meeting()
    mid = meeting["meetingId"]
    for slug in ("jamie", "tom", "nick"):
        db.record_attendance_report(mid, clubdb.lookup_member_id(slug), "yes")
    for slug in meeting["pickerSlugs"]:
        db.record_attendance_report(mid, clubdb.lookup_member_id(slug), "no")

    status = meeting_rules.meeting_status(mid)
    assert "picker_unavailable" in status["risks"]
    assert status["recommendation"] == "needs_attention"


def test_meeting_status_ready_when_quorum_and_picker(fresh_db):
    from agent import clubdb, db
    from agent.club import meeting_rules

    meeting = meeting_rules.next_meeting()
    mid = meeting["meetingId"]
    yes = set(meeting["pickerSlugs"])
    yes.update(["jamie", "tom", "nick"])
    for slug in sorted(yes):
        db.record_attendance_report(mid, clubdb.lookup_member_id(slug), "yes")

    status = meeting_rules.meeting_status(mid)
    assert status["hasQuorum"] is True
    assert status["pickerAvailable"] is True
    assert status["recommendation"] == "ready"
