"""scheduler.due_notifications — all branches with synthesized corpus + time."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


def _fake_book(title, slug, *, picker=None, date=None, placeholder=False):
    return {
        "slug": slug, "title": title, "authors": ["A"],
        "meetingDate": date, "placeholder": placeholder, "pickerName": picker,
    }


class TestDueNotifications:
    def test_meeting_reminder_within_window(self, monkeypatch):
        """A placeholder meeting 2 days out should produce a reminder."""
        from agent import scheduler
        now = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)
        books = [_fake_book("Patterns in Nature", "patterns", picker="Tom",
                            date="2026-06-30T00:00:00Z", placeholder=True)]
        monkeypatch.setattr(scheduler.cr, "books", lambda: books)
        due = scheduler.due_notifications(now, set())
        keys = [k for k, _ in due]
        assert "meeting-patterns-soon" in keys

    def test_meeting_reminder_outside_window(self, monkeypatch):
        """A placeholder meeting 10 days out should NOT produce a reminder yet."""
        from agent import scheduler
        now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        books = [_fake_book("Far Future", "far", picker="Tom",
                            date="2026-06-30T00:00:00Z", placeholder=True)]
        monkeypatch.setattr(scheduler.cr, "books", lambda: books)
        due = scheduler.due_notifications(now, set())
        assert not [k for k, _ in due if k.startswith("meeting-")]

    def test_review_nudge_recent_read(self, monkeypatch):
        """A non-placeholder book read within 30 days should produce a review nudge."""
        from agent import scheduler
        now = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)
        books = [_fake_book("Just Read", "just-read", picker="Erik",
                            date="2026-06-20T00:00:00Z", placeholder=False)]
        monkeypatch.setattr(scheduler.cr, "books", lambda: books)
        due = scheduler.due_notifications(now, set())
        assert any(k == "review-nudge-just-read" for k, _ in due)

    def test_milestone_at_multiple_of_25(self, monkeypatch):
        from agent import scheduler
        now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        books = [_fake_book(f"Book {i}", f"book-{i}", date="2025-01-01T00:00:00Z")
                 for i in range(25)]
        monkeypatch.setattr(scheduler.cr, "books", lambda: books)
        due = scheduler.due_notifications(now, set())
        assert any(k == "milestone-books-25" for k, _ in due)

    def test_no_milestone_at_non_multiple(self, monkeypatch):
        from agent import scheduler
        now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        books = [_fake_book(f"Book {i}", f"book-{i}", date="2025-01-01T00:00:00Z")
                 for i in range(26)]
        monkeypatch.setattr(scheduler.cr, "books", lambda: books)
        due = scheduler.due_notifications(now, set())
        assert not [k for k, _ in due if k.startswith("milestone-")]

    def test_anniversary_fires_in_april(self, monkeypatch):
        from agent import scheduler
        monkeypatch.setattr(scheduler.cr, "books", lambda: [])
        now_april = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
        due = scheduler.due_notifications(now_april, set())
        assert any(k == "anniversary-2026" for k, _ in due)

    def test_no_anniversary_outside_april(self, monkeypatch):
        from agent import scheduler
        monkeypatch.setattr(scheduler.cr, "books", lambda: [])
        now_may = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        due = scheduler.due_notifications(now_may, set())
        assert not any(k.startswith("anniversary-") for k, _ in due)

    def test_dedup_via_already_sent(self, monkeypatch):
        """Keys in already_sent are filtered out."""
        from agent import scheduler
        monkeypatch.setattr(scheduler.cr, "books", lambda: [])
        now = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
        first = scheduler.due_notifications(now, set())
        sent_keys = {k for k, _ in first}
        second = scheduler.due_notifications(now, sent_keys)
        assert second == []
