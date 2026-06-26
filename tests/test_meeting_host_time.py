"""Meeting host (distinct from a book's picker) and local date/time are surfaced."""
import datetime

from agent import clubdb, db


def test_hosts_for_meeting_returns_meeting_host(fresh_db):
    # a-world-appears (the upcoming meeting) was hosted by jamie.
    mid = clubdb.meeting_id_for_book_slug("a-world-appears")
    hosts = clubdb.hosts_for_meeting(mid)
    assert [h["slug"] for h in hosts] == ["jamie"]
    assert clubdb.hosts_for_meeting(None) == []


def test_books_expose_meeting_host_and_local_start_time(reset_books_cache):
    from agent import corpus_read as cr
    full = cr.find_book("a-world-appears")               # full enriched dict
    # picker (book-level) and host (meeting-level) are both available and, here, the same.
    assert full["pickerNames"]
    assert full["meetingHostNames"]                      # who hosted is surfaced
    assert full["meetingStartTime"] == "18:30"           # local time, not UTC
    assert full["meetingDate"] == "2026-06-30"           # local date


def test_club_migration_converts_utc_meeting_date_to_local(fresh_db):
    # A winter evening meeting stored as next-day UTC must normalize to the true local day.
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO club_meetings(id, date, placeholder) VALUES (9001, ?, 0)",
            ("2026-01-28T01:00:00.000Z",),   # 7:00pm CST on Jan 27 local
        )
    clubdb.ensure_schema()                    # runs _migrate_club
    with db.connect() as conn:
        row = conn.execute("SELECT date, start_time FROM club_meetings WHERE id = 9001").fetchone()
    assert row["date"] == "2026-01-27"
    assert row["start_time"] == "19:00"
    # idempotent: a second run leaves the already-local row untouched
    clubdb.ensure_schema()
    with db.connect() as conn:
        again = conn.execute("SELECT date FROM club_meetings WHERE id = 9001").fetchone()
    assert again["date"] == "2026-01-27"
    # sanity: zoneinfo is doing the work we expect
    assert datetime.date.fromisoformat(row["date"]).month == 1
