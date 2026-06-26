"""Oliver's contextual email signature draws the next read + a fun fact from the corpus."""
import random
from datetime import date

from agent.mail import signature


def _setup(monkeypatch, *, upcoming=None, stats=None, books=None):
    monkeypatch.setattr(signature.cr, "upcoming_meetings", lambda: upcoming or [])
    monkeypatch.setattr(signature.cr, "club_stats", lambda: stats or {})
    monkeypatch.setattr(signature.cr, "books", lambda: books or [])


def test_signature_includes_oliver_next_book_and_a_fact(monkeypatch):
    _setup(
        monkeypatch,
        upcoming=[{"title": "Stiff", "meetingDate": "2026-07-28T00:00:00Z", "pickedBy": "Tom"}],
        stats={"totalRead": 179, "nonfiction": 158, "fiction": 21,
               "totalPages": 50000, "pickerLeaderboard": [("Jamie", 40)]},
        books=[],
    )
    sig = signature.email_signature(today=date(2026, 6, 25), rng=random.Random(0))
    lines = sig.splitlines()
    assert lines[0] == "— Oliver"
    assert "Next up: Stiff" in lines[1]
    assert "picked by Tom" in lines[1]
    assert "July 28" in lines[1]  # friendly date, not the ISO form
    assert len(lines) == 3  # Oliver + next-up + one fun fact


def test_signature_without_upcoming_still_signs_with_a_fact(monkeypatch):
    _setup(monkeypatch, upcoming=[], stats={"totalRead": 179}, books=[])
    sig = signature.email_signature(today=date(2026, 6, 25), rng=random.Random(1))
    assert sig.startswith("— Oliver")
    assert "179 books read together since 2003" in sig


def test_fun_facts_years_ago_this_month(monkeypatch):
    facts = signature._fun_facts(
        {"totalRead": 1},
        [{"isRead": True, "meetingDate": "2016-06-15T00:00:00Z", "title": "Old Read"}],
        date(2026, 6, 25),
    )
    assert any("10 years ago this month we read Old Read" in f for f in facts)
