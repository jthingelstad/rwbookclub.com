"""Hosting history surfacing: member_history / club_stats / get_book expose who hosted
meetings, and a book's picker is DERIVED from the host of its meeting(s) (club_book_pickers
view — host is the single source of truth; see clubdb)."""

from __future__ import annotations

import pytest

from agent import clubdb, db
from agent import corpus_read as cr

pytestmark = pytest.mark.usefixtures("fresh_db")


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


def test_book_picker_derives_from_meeting_host():
    """A book's picker is DERIVED from the host of its meeting — set the meeting's host and the
    book's picker (club_book_pickers view) follows. There is no separate picker to set."""
    with db.connect() as conn:
        bid = clubdb._next_id(conn, "club_books")
        conn.execute("INSERT INTO club_books(id, slug, title) VALUES (?,?,?)",
                     (bid, "derived-picker-book", "Derived Picker Book"))
        mid = clubdb._next_id(conn, "club_meetings")
        conn.execute("INSERT INTO club_meetings(id, date) VALUES (?, '2020-01-01')", (mid,))
        conn.execute("INSERT INTO club_meeting_books(meeting_id, book_id, ordinal) VALUES (?,?,0)",
                     (mid, bid))
        member_id = conn.execute("SELECT id FROM club_members LIMIT 1").fetchone()["id"]
        # No picker set directly; making the member the meeting's host makes them the picker.
        clubdb.set_meeting_hosts(conn, mid, [member_id])
        picker = conn.execute(
            "SELECT member_id FROM club_book_pickers WHERE book_id=?", (bid,)
        ).fetchone()
        assert picker is not None and picker["member_id"] == member_id


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
