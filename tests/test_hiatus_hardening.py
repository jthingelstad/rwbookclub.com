"""Enrichment sweep (retry caps + exhaustion flags) and the health facts the morning digest reads."""

import json

import pytest

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
        conn.execute(
            "INSERT INTO club_book_enrichment (book_id, enriched_at, enrich_attempts) "
            "VALUES (2, datetime('now','-60 days'), 0)"
        )
    books = [
        {
            "id": 1,
            "slug": "new-book",
            "title": "New",
            "synopsis": "has one",
            "publication_year": 2020,
            "page_count": 300,
        },
        {
            "id": 2,
            "slug": "gappy-book",
            "title": "Gappy",
            "synopsis": None,
            "publication_year": 2001,
            "page_count": 200,
        },
    ]
    calls = _mini_world(monkeypatch, books=books)
    summary = enrich_loop.run_pending(limit=8, fetch_images=False)
    assert calls == ["new-book", "gappy-book"]  # new first, then the retry
    assert summary["enriched"] == 1 and summary["retried"] == 1
    with db.connect() as conn:
        # the retry left the gap → attempt burned
        att = conn.execute(
            "SELECT enrich_attempts FROM club_book_enrichment WHERE book_id=2"
        ).fetchone()[0]
    assert att == 1


def test_sweep_flags_exhausted_and_parks(fresh_db, monkeypatch):
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO club_book_enrichment (book_id, enriched_at, enrich_attempts) "
            "VALUES (2, datetime('now','-60 days'), 2)"
        )  # one retry left
    books = [
        {
            "id": 2,
            "slug": "hopeless-book",
            "title": "Hopeless",
            "synopsis": None,
            "publication_year": None,
            "page_count": None,
        }
    ]
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
        conn.execute(
            "INSERT OR REPLACE INTO club_book_enrichment (book_id, enriched_at, enrich_attempts) "
            "VALUES (2, datetime('now','-60 days'), 1)"
        )
    books = [
        {
            "id": 2,
            "slug": "gappy",
            "title": "G",
            "synopsis": None,
            "publication_year": None,
            "page_count": None,
        }
    ]
    _mini_world(monkeypatch, books=books, enrich_result="raise")
    summary = enrich_loop.run_pending(limit=8, fetch_images=False)
    assert summary == {"enriched": 0, "retried": 0, "exhausted": []}
    with db.connect() as conn:
        att = conn.execute(
            "SELECT enrich_attempts FROM club_book_enrichment WHERE book_id=2"
        ).fetchone()[0]
    assert att == 1  # unchanged — a down source is not the book's fault


# ── Health facts (read by the Otto Ops morning digest) ─────────────────────────
def test_healthy_facts_have_no_problems(fresh_db):
    from agent import clock

    fresh_db.set_job_state(
        "offsite_backup", {"date": clock.club_today_iso(), "file": "oliver-today.db.gz"}
    )
    facts = health.snapshot()
    # A same-day backup must read as healthy (regression: `0 or 99` once flagged 0d as stale).
    assert facts["backupAgeDays"] == 0
    assert health.problems(facts) == []


def test_problems_name_a_stale_backup_and_count_warnings_without_their_text(fresh_db):
    fresh_db.set_job_state("offsite_backup", {"date": "2026-06-20", "file": "old.gz"})
    fresh_db.add_activity("warning", "Something broke for a member", "details")
    found = health.problems(health.snapshot())
    assert any(p.startswith("offsite backup is") for p in found)
    assert "1 warnings in 7 days (#oliver-log)" in found
    assert not any("Something broke" in p for p in found)


@pytest.mark.parametrize(("backup_date", "stale"), [("2026-06-22", False), ("2026-06-21", True)])
def test_backup_freshness_matches_weekly_cadence(fresh_db, backup_date, stale):
    fresh_db.set_job_state("offsite_backup", {"date": backup_date, "file": "snapshot.gz"})
    found = health.problems(health.snapshot())
    assert any(p.startswith("offsite backup is") for p in found) is stale


def test_json_command_prints_facts_and_problems_and_carries_no_titles(fresh_db, capsys):
    fresh_db.add_activity("warning", "Private warning title", "details")
    assert health.main(["--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"facts", "problems"}
    assert out["facts"]["warnings7d"] == 1
    assert {"meetingScheduled", "bookPicked", "daysToMeeting"} <= set(out["facts"])
    assert "Private warning title" not in json.dumps(out)


def test_retired_email_job_no_longer_reads_as_overdue(fresh_db):
    from agent import database

    fresh_db.begin_job_run(
        "health_digest",
        lease_owner="old-worker",
        lease_expires_at="2026-01-01T00:00:00+00:00",
        expected_interval_seconds=3600,
        now="2026-01-01T00:00:00+00:00",
    )
    assert "health_digest" in [row["job_name"] for row in fresh_db.job_statuses()]
    with fresh_db.connect() as conn:
        database._retire_health_digest_job(conn)
    assert "health_digest" not in [row["job_name"] for row in fresh_db.job_statuses()]


def test_json_command_refuses_a_missing_database(monkeypatch, tmp_path):
    missing = tmp_path / "oliver.db"
    monkeypatch.setattr(health.db, "DB_PATH", missing)
    with pytest.raises(SystemExit) as stop:
        health.main(["--json"])
    assert stop.value.code == 2
    assert not missing.exists()
