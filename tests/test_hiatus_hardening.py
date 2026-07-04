"""Enrichment sweep (retry caps + exhaustion flags) and the weekly health digest."""

from datetime import datetime
from zoneinfo import ZoneInfo

from agent import db, health
from agent.enrich import loop as enrich_loop


# ── Enrichment sweep ─────────────────────────────────────────────────────────
def _mini_world(monkeypatch, *, books, enrich_result=None):
    """A tiny in-memory enrichment world: fake all_books/all_authors + enrich fns."""
    monkeypatch.setattr(enrich_loop.clubdb, "all_books", lambda conn: books)
    monkeypatch.setattr(enrich_loop.clubdb, "all_authors", lambda conn: [])
    calls = []

    def fake_enrich(conn, entity, **kw):
        calls.append(entity["slug"])
        if enrich_result == "raise":
            raise ConnectionError("OL down")
        return {}

    monkeypatch.setattr(enrich_loop, "enrich_book", fake_enrich)
    return calls


def test_sweep_enriches_new_then_retries_incomplete(fresh_db, monkeypatch):
    with db.connect() as conn:
        # The fixture seeds enrichment for every real book — clear our two ids so the world is
        # ours: book 1 never enriched (new); book 2 enriched long ago, still missing synopsis.
        conn.execute("DELETE FROM club_book_enrichment WHERE book_id IN (1, 2)")
        conn.execute("INSERT INTO club_book_enrichment (book_id, enriched_at, enrich_attempts) "
                     "VALUES (2, datetime('now','-60 days'), 0)")
    books = [
        {"id": 1, "slug": "new-book", "title": "New", "synopsis": "has one",
         "publication_year": 2020, "page_count": 300},
        {"id": 2, "slug": "gappy-book", "title": "Gappy", "synopsis": None,
         "publication_year": 2001, "page_count": 200},
    ]
    calls = _mini_world(monkeypatch, books=books)
    summary = enrich_loop.run_pending(limit=8, fetch_images=False)
    assert calls == ["new-book", "gappy-book"]   # new first, then the retry
    assert summary["enriched"] == 1 and summary["retried"] == 1
    with db.connect() as conn:
        # the retry left the gap → attempt burned
        att = conn.execute("SELECT enrich_attempts FROM club_book_enrichment WHERE book_id=2").fetchone()[0]
    assert att == 1


def test_sweep_flags_exhausted_and_parks(fresh_db, monkeypatch):
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO club_book_enrichment (book_id, enriched_at, enrich_attempts) "
                     "VALUES (2, datetime('now','-60 days'), 2)")  # one retry left
    books = [{"id": 2, "slug": "hopeless-book", "title": "Hopeless", "synopsis": None,
              "publication_year": None, "page_count": None}]
    _mini_world(monkeypatch, books=books)
    summary = enrich_loop.run_pending(limit=8, fetch_images=False)
    assert summary["exhausted"] == ["hopeless-book"]
    acts = fresh_db.pending_activity(limit=5)
    assert any("Enrichment exhausted: hopeless-book" in a["title"] for a in acts)
    # Parked: the next sweep must not select it again.
    calls2 = _mini_world(monkeypatch, books=books)
    enrich_loop.run_pending(limit=8, fetch_images=False)
    assert calls2 == []


def test_sweep_network_failure_aborts_without_burning_attempts(fresh_db, monkeypatch):
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO club_book_enrichment (book_id, enriched_at, enrich_attempts) "
                     "VALUES (2, datetime('now','-60 days'), 1)")
    books = [{"id": 2, "slug": "gappy", "title": "G", "synopsis": None,
              "publication_year": None, "page_count": None}]
    _mini_world(monkeypatch, books=books, enrich_result="raise")
    summary = enrich_loop.run_pending(limit=8, fetch_images=False)
    assert summary == {"enriched": 0, "retried": 0, "exhausted": []}
    with db.connect() as conn:
        att = conn.execute("SELECT enrich_attempts FROM club_book_enrichment WHERE book_id=2").fetchone()[0]
    assert att == 1  # unchanged — a down source is not the book's fault


# ── Health digest ────────────────────────────────────────────────────────────
def _monday_8am():
    return datetime(2026, 7, 6, 8, 30, tzinfo=ZoneInfo("America/Chicago"))


def test_digest_sends_once_per_week_at_the_gate(fresh_db, monkeypatch):
    sent = []
    monkeypatch.setattr(health.outbound, "send",
                        lambda **kw: sent.append(kw) or {"emailId": "x"})
    monkeypatch.setattr(health.db, "member_slug_for_user", lambda uid: "jamie")
    monkeypatch.setattr(health.db, "email_for_member",
                        lambda slug: {"email": "jamie@example.test"})
    monkeypatch.setattr(health.oliver, "compose",
                        lambda kind, facts, fallback="": fallback)
    fresh_db.set_job_state("offsite_backup", {"date": "2026-07-06", "file": "oliver-2026-07-06.db.gz"})

    assert health.run(_monday_8am()) is True
    assert sent[0]["to"] == ["jamie@example.test"]
    assert "Backup" in sent[0]["body"] and "absence is the alarm" in sent[0]["body"]
    assert health.run(_monday_8am()) is False        # same week → no repeat
    assert health.run(_monday_8am().replace(hour=14)) is False  # outside the gate hour


def test_digest_flags_stale_backup_and_warnings(fresh_db, monkeypatch):
    monkeypatch.setattr(health.oliver, "compose", lambda kind, facts, fallback="": fallback)
    fresh_db.set_job_state("offsite_backup", {"date": "2026-06-20", "file": "old.gz"})
    fresh_db.add_activity("warning", "Something broke", "details")
    subject, body = health.digest_email(health.snapshot())
    assert "⚠️" in subject
    assert "Something broke" in body
