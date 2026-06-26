"""Hosting history surfacing (Phase 6, slice A): member_history / club_stats / get_book expose
who hosted meetings, and the migration backfills host = picker for book-meetings missing a host."""

from __future__ import annotations

from agent import clubdb, db
from agent import corpus_read as cr


def test_club_stats_has_host_leaderboard(reset_books_cache):
    stats = cr.club_stats()
    lb = stats["hostLeaderboard"]
    assert lb and lb[0][1] >= 1                       # at least one member has hosted
    # Consistency: the top host's leaderboard count matches member_history.
    top_name, top_count = lb[0]
    mh = cr.member_history(top_name)
    assert mh["hostedCount"] == top_count == len(mh["hosted"])


def test_member_history_includes_hosting(reset_books_cache):
    top_name = cr.club_stats()["hostLeaderboard"][0][0]
    mh = cr.member_history(top_name)
    assert mh["hostedCount"] == len(mh["hosted"]) > 0
    assert all("books" in h and "date" in h for h in mh["hosted"])
    # hosted is most-recent first
    dates = [h["date"] or "" for h in mh["hosted"]]
    assert dates == sorted(dates, reverse=True)


def test_get_book_surfaces_host(reset_books_cache):
    b = cr.get_book("the-martian")
    assert b is not None and "host" in b  # list of host names (or None if unhosted)


def test_migrate_backfills_host_from_picker():
    """A meeting with a book + picker but no host gets host == the book's picker."""
    with db.connect() as conn:
        bid = clubdb._next_id(conn, "club_books")
        conn.execute("INSERT INTO club_books(id, slug, title) VALUES (?,?,?)",
                     (bid, "host-backfill-book", "Host Backfill Book"))
        mid = clubdb._next_id(conn, "club_meetings")
        conn.execute("INSERT INTO club_meetings(id, date) VALUES (?, '2020-01-01')", (mid,))
        conn.execute("INSERT INTO club_meeting_books(meeting_id, book_id, ordinal) VALUES (?,?,0)",
                     (mid, bid))
        member_id = conn.execute("SELECT id FROM club_members LIMIT 1").fetchone()["id"]
        conn.execute("INSERT INTO club_book_pickers(book_id, member_id, ordinal) VALUES (?,?,0)",
                     (bid, member_id))
        assert conn.execute(
            "SELECT COUNT(*) c FROM club_meeting_hosts WHERE meeting_id=?", (mid,)
        ).fetchone()["c"] == 0

    clubdb.ensure_schema()  # runs _migrate_club → host backfill

    with db.connect() as conn:
        host = conn.execute(
            "SELECT member_id FROM club_meeting_hosts WHERE meeting_id=?", (mid,)
        ).fetchone()
        assert host is not None and host["member_id"] == member_id


def test_migrate_does_not_invent_host_without_picker():
    """A book-meeting whose book has no picker stays host-less (nothing to copy)."""
    with db.connect() as conn:
        bid = clubdb._next_id(conn, "club_books")
        conn.execute("INSERT INTO club_books(id, slug, title) VALUES (?,?,?)",
                     (bid, "no-picker-book", "No Picker Book"))
        mid = clubdb._next_id(conn, "club_meetings")
        conn.execute("INSERT INTO club_meetings(id, date) VALUES (?, '2020-02-01')", (mid,))
        conn.execute("INSERT INTO club_meeting_books(meeting_id, book_id, ordinal) VALUES (?,?,0)",
                     (mid, bid))

    clubdb.ensure_schema()

    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM club_meeting_hosts WHERE meeting_id=?", (mid,)
        ).fetchone()["c"] == 0
