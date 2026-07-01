"""scheduler.due_notifications — all branches with synthesized corpus + time."""

from __future__ import annotations

from datetime import datetime, timezone


def _fake_book(title, slug, *, picker=None, date=None, upcoming=False):
    return {
        "slug": slug, "title": title, "authors": ["A"],
        "meetingDate": date, "pickerName": picker,
        "isUpcoming": upcoming,
        "isRead": bool(date and not upcoming),
    }


class TestDueNotifications:
    def test_meeting_reminder_within_window(self, monkeypatch):
        """An upcoming meeting 2 days out should produce a reminder."""
        from agent import scheduler
        now = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)
        books = [_fake_book("Patterns in Nature", "patterns", picker="Tom",
                            date="2026-06-30T00:00:00Z", upcoming=True)]
        monkeypatch.setattr(scheduler.cr, "books", lambda: books)
        due = scheduler.due_notifications(now, set())
        keys = [n.key for n in due]
        assert "meeting-patterns-soon" in keys
        # The facts carry the structured data Oliver voices; fallback is the template.
        note = next(n for n in due if n.key == "meeting-patterns-soon")
        assert note.facts["book"] == "Patterns in Nature"
        assert note.facts["picker"] == "Tom"
        assert "Patterns in Nature" in note.fallback

    def test_meeting_reminder_outside_window(self, monkeypatch):
        """An upcoming meeting 10 days out should NOT produce a reminder yet."""
        from agent import scheduler
        now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        books = [_fake_book("Far Future", "far", picker="Tom",
                            date="2026-06-30T00:00:00Z", upcoming=True)]
        monkeypatch.setattr(scheduler.cr, "books", lambda: books)
        due = scheduler.due_notifications(now, set())
        assert not [n for n in due if n.key.startswith("meeting-")]

    def test_review_nudge_recent_read(self, monkeypatch):
        """A past (read) book within 30 days should produce a review nudge."""
        from agent import scheduler
        now = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)
        books = [_fake_book("Just Read", "just-read", picker="Erik",
                            date="2026-06-20T00:00:00Z", upcoming=False)]
        monkeypatch.setattr(scheduler.cr, "books", lambda: books)
        due = scheduler.due_notifications(now, set())
        assert any(n.key == "review-nudge-just-read" for n in due)

    def test_milestone_at_multiple_of_25(self, monkeypatch):
        from agent import scheduler
        now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        books = [_fake_book(f"Book {i}", f"book-{i}", date="2025-01-01T00:00:00Z")
                 for i in range(25)]
        monkeypatch.setattr(scheduler.cr, "books", lambda: books)
        due = scheduler.due_notifications(now, set())
        assert any(n.key == "milestone-books-25" for n in due)

    def test_no_milestone_at_non_multiple(self, monkeypatch):
        from agent import scheduler
        now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        books = [_fake_book(f"Book {i}", f"book-{i}", date="2025-01-01T00:00:00Z")
                 for i in range(26)]
        monkeypatch.setattr(scheduler.cr, "books", lambda: books)
        due = scheduler.due_notifications(now, set())
        assert not [n for n in due if n.key.startswith("milestone-")]

    def test_anniversary_fires_in_april(self, monkeypatch):
        from agent import scheduler
        monkeypatch.setattr(scheduler.cr, "books", lambda: [])
        now_april = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
        due = scheduler.due_notifications(now_april, set())
        assert any(n.key == "anniversary-2026" for n in due)

    def test_no_anniversary_outside_april(self, monkeypatch):
        from agent import scheduler
        monkeypatch.setattr(scheduler.cr, "books", lambda: [])
        now_may = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        due = scheduler.due_notifications(now_may, set())
        assert not any(n.key.startswith("anniversary-") for n in due)

    def test_dedup_via_already_sent(self, monkeypatch):
        """Keys in already_sent are filtered out."""
        from agent import scheduler
        monkeypatch.setattr(scheduler.cr, "books", lambda: [])
        now = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
        first = scheduler.due_notifications(now, set())
        sent_keys = {n.key for n in first}
        second = scheduler.due_notifications(now, sent_keys)
        assert second == []
