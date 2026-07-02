"""Archive memory mining: per-year replay, resume cursor, boundary, and the db feed."""

from __future__ import annotations

from agent import db
from agent.script import mine_archive_memories as miner


def _mail(mid: str, slug: str, email: str, sent_at: str, body: str = "I loved that book."):
    db.upsert_mail_message({
        "message_id": mid, "thread_id": f"t-{mid}", "from_email": email, "from_name": slug.title(),
        "member_slug": slug, "subject": "Books", "sent_at": sent_at, "received_at": sent_at,
        "body_text": body, "body_clean": body,
    })


def test_mail_messages_between_bounds(fresh_db):
    _mail("a", "jamie", "j@x.com", "2018-06-01T00:00:00Z")
    _mail("b", "jamie", "j@x.com", "2019-06-01T00:00:00Z")
    _mail("c", "tom", "t@x.com", "2018-07-01T00:00:00Z")
    _mail("d", "jamie", "oliver@rwbookclub.com", "2018-08-01T00:00:00Z")  # Oliver's outbound
    rows = db.mail_messages_between("2018-01-01", "2019-01-01",
                                    member_slug="jamie", exclude_from="oliver@rwbookclub.com")
    assert [r["message_id"] for r in rows] == ["a"]     # 2019 out of range; tom filtered; oliver excluded
    both = db.mail_messages_between("2018-01-01", "2019-01-01", exclude_from="oliver@rwbookclub.com")
    assert {r["message_id"] for r in both} == {"a", "c"}  # unfiltered = the club lane feed


def test_lane_replays_years_and_resumes(fresh_db, monkeypatch):
    _mail("m18", "tom", "t@x.com", "2018-03-01T00:00:00Z")
    _mail("m19", "tom", "t@x.com", "2019-03-01T00:00:00Z")
    calls = []

    def fake_consolidate(lines, *, scope, subject=None, era_note=None, dry_run=False, **kw):
        calls.append((subject, era_note))
        return {"add": 1, "update": 0, "retire": 0}

    monkeypatch.setattr(miner.reflection, "consolidate", fake_consolidate)
    counts = miner._mine_lane("tom", until="2026-01-01", years=[2018, 2019, 2020], dry_run=False)
    assert counts["years"] == 2 and counts["add"] == 2
    assert [c[0] for c in calls] == ["tom", "tom"]
    assert "2018" in calls[0][1] and "2019" in calls[1][1]      # era notes carry the year
    state = db.get_job_state(miner.JOB_KEY)
    assert state["done"]["tom"] == 2020                          # empty 2020 also advanced

    calls.clear()                                                # resume: nothing left to do
    miner._mine_lane("tom", until="2026-01-01", years=[2018, 2019, 2020], dry_run=False)
    assert calls == []


def test_lane_stops_on_failure_and_resumes_there(fresh_db, monkeypatch):
    _mail("m18", "tom", "t@x.com", "2018-03-01T00:00:00Z")
    _mail("m19", "tom", "t@x.com", "2019-03-01T00:00:00Z")
    monkeypatch.setattr(miner.reflection, "consolidate",
                        lambda *a, **k: {"skipped": "unparseable"})
    counts = miner._mine_lane("tom", until="2026-01-01", years=[2018, 2019], dry_run=False)
    assert counts["years"] == 0
    state = db.get_job_state(miner.JOB_KEY) or {}
    assert (state.get("done") or {}).get("tom", 0) < 2018        # cursor NOT past the failed year


def test_until_boundary_respected(fresh_db, monkeypatch):
    _mail("in", "tom", "t@x.com", "2026-05-01T00:00:00Z")
    _mail("out", "tom", "t@x.com", "2026-06-15T00:00:00Z")       # after the boundary
    seen = []
    monkeypatch.setattr(miner.reflection, "consolidate",
                        lambda lines, **kw: seen.extend(lines) or {"add": 0, "update": 0, "retire": 0})
    miner._mine_lane("tom", until="2026-06-01T00:00:00Z", years=[2026], dry_run=False)
    assert len(seen) == 1                                        # only the in-range message was fed
    assert "Books" in seen[0]


def test_club_lane_feeds_all_members(fresh_db, monkeypatch):
    _mail("j1", "jamie", "j@x.com", "2018-03-01T00:00:00Z", "jamie says hi")
    _mail("t1", "tom", "t@x.com", "2018-04-01T00:00:00Z", "tom says hi")
    captured = {}

    def fake_consolidate(lines, *, scope, subject=None, **kw):
        captured.update({"scope": scope, "subject": subject, "n": len(lines)})
        return {"add": 0, "update": 0, "retire": 0}

    monkeypatch.setattr(miner.reflection, "consolidate", fake_consolidate)
    miner._mine_lane(miner.CLUB_LANE, until="2026-01-01", years=[2018], dry_run=False)
    assert captured == {"scope": "club", "subject": None, "n": 2}


def test_dry_run_leaves_no_cursor(fresh_db, monkeypatch):
    _mail("m18", "tom", "t@x.com", "2018-03-01T00:00:00Z")
    monkeypatch.setattr(miner.reflection, "consolidate",
                        lambda *a, **k: {"add": 1, "update": 0, "retire": 0})
    miner._mine_lane("tom", until="2026-01-01", years=[2018], dry_run=True)
    assert db.get_job_state(miner.JOB_KEY) is None