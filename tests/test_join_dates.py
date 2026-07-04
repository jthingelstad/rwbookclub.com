"""Member join dates: backfill, create-member stamp, and pre-join ask filtering everywhere."""


from agent import clock, clubdb, db
from agent import corpus_read as cr
from agent.club import review_drive as rd


def _joined(slug):
    with db.connect() as conn:
        return conn.execute("SELECT joined FROM club_members WHERE slug=?", (slug,)).fetchone()[0]


def test_backfill_and_corrections_in_fixture():
    # The fixture is a live snapshot post-migration: first-pick backfill + the two corrections.
    assert _joined("erik") == "2003-09-26"
    assert _joined("jamie") == "2007-12-26"     # Beautiful Evidence, not his first pick
    assert _joined("j-rauser") is None          # no hosted meetings → unknown stays unknown


def test_create_member_stamps_today():
    with db.connect() as conn:
        res = clubdb.create_member(conn, "Join Test Member")
        joined = conn.execute("SELECT joined FROM club_members WHERE id=?",
                              (res["id"],)).fetchone()[0]
        conn.execute("DELETE FROM club_members WHERE id=?", (res["id"],))
    assert joined == clock.club_today_iso()


def test_review_drive_never_asks_about_prejoin_books(fresh_db):
    # nick joined 2014; rate him a 2003-era book — it must never become a candidate.
    with db.connect() as conn:
        mid = clubdb.lookup_member_id("nick")
        conn.execute("DELETE FROM club_reviews WHERE member_id=?", (mid,))
        bid = conn.execute(
            "SELECT b.id FROM club_books b JOIN club_meeting_books mb ON mb.book_id=b.id "
            "JOIN club_meetings m ON m.id=mb.meeting_id WHERE m.date < '2014-01-01' "
            "LIMIT 1").fetchone()[0]
        clubdb.upsert_review(conn, book_id=bid, member_id=mid, rating=5, body=None)
    assert rd.next_candidate("nick") is None
    with db.connect() as conn:  # clear joined → the same book becomes fair game (fail open)
        conn.execute("UPDATE club_members SET joined=NULL WHERE id=?", (mid,))
    assert rd.next_candidate("nick") is not None


def test_pending_reviews_respects_tenure(reset_books_cache):
    # jamie joined 2007-12-26 — nothing read in 2003-2007 (pre-Beautiful Evidence) is owed.
    pending = cr.pending_reviews("jamie")
    dates = [b.get("yearRead") for b in pending["books"] if b.get("yearRead")]
    assert dates and min(dates) >= 2007
    # erik (joined 2003) still owes for the early years — the filter is per-member.
    erik = cr.pending_reviews("erik")
    assert any((b.get("yearRead") or 9999) < 2007 for b in erik["books"])


def test_home_dashboard_unrated_respects_tenure(fresh_db):
    from agent.webapp import routes_member
    with db.connect() as conn:
        mid = clubdb.lookup_member_id("nick")
        conn.execute("DELETE FROM club_reviews WHERE member_id=?", (mid,))
    ctx = routes_member.home_context("nick")
    years = [int(b["year"]) for b in ctx["unrated"] if b["year"]]
    assert years and min(years) >= 2014


def test_tenure_filter_on_grid_rows():
    from agent.webapp.routes_member import _tenure
    rows = [{"slug": "old", "date": "2005-01-01"}, {"slug": "new", "date": "2020-01-01"},
            {"slug": "undated", "date": None}]
    kept = _tenure("nick", rows)  # nick joined 2014
    assert [r["slug"] for r in kept] == ["new", "undated"]  # fail open on missing dates
    assert _tenure("erik", rows) == rows  # 2003 joiner sees everything
