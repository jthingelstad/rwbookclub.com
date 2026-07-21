"""Shared pytest setup for the Oliver test suite.

Sets `OLIVER_DB_PATH` to a temp file BEFORE any agent module is imported, so
the schema gets created in a scratch location instead of the live `agent/oliver.db`.
Provides a `fresh_db` fixture that truncates all tables between tests.
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
import tempfile
from contextlib import closing

import pytest

# Set the env vars BEFORE any agent module gets a chance to import db / corpus.paths.
_TMP_DIR = pathlib.Path(tempfile.mkdtemp(prefix="oliver-tests-"))
os.environ["OLIVER_DB_PATH"] = str(_TMP_DIR / "test.db")
_PRISTINE_DB = _TMP_DIR / "pristine.db"
_PRISTINE_CORPUS_SIG: tuple | None = None
# Redirect the corpus to a temp dir so a test run never touches the developer's real
# (gitignored) corpus/data — the session fixture regenerates it here from the SQL fixture.
os.environ["OLIVER_CORPUS_DIR"] = str(_TMP_DIR / "corpus")
# Keep /oliver add-book offline in tests — no inline external enrichment (network).
os.environ["OLIVER_ENRICH_ON_WRITE"] = "0"
# Admin-authorization tests must not depend on the developer's private .env. Set a realistic,
# nonzero test snowflake before agent.config is imported; python-dotenv leaves explicit env values
# untouched, so the same identity is used locally and in clean GitHub runners.
os.environ["DISCORD_ADMIN_USER_ID"] = "999000000000000001"
# Give the web app a real (test) signing secret so session-cookie tests work and the
# fail-closed guard in server.ensure_running doesn't trip in CI (which has no bot token).
os.environ.setdefault("WEBAPP_SECRET", "test-webapp-signing-secret-not-the-dev-default")

# Public-safe club_* seed (no PII) — replaces seeding from the (now-gitignored) corpus.
# Regenerate with: python -m agent.script.dump_club_seed > tests/fixtures/club_seed.sql
_FIXTURE_SQL = (pathlib.Path(__file__).parent / "fixtures" / "club_seed.sql").read_text()

# Tables that FK into club_members / club_meetings — cleared before club_* so deleting club
# rows can't trip a foreign-key constraint from a prior test's leftover rows.
_FK_DEPENDENTS = (
    "events",
    "meeting_member_status",
    "mail_message_fts",
    "mail_messages",
    "member_identities",
    "member_preferences",
)


def _reseed_club(conn) -> None:
    """Clear FK-dependents + club_* and replay the public-safe fixture (parents-first)."""
    from agent import clubdb

    for t in _FK_DEPENDENTS:
        conn.execute(f"DELETE FROM {t}")
    for t in reversed(clubdb.CLUB_TABLES):
        conn.execute(f"DELETE FROM {t}")
    conn.executescript(_FIXTURE_SQL)


def _backup_database(source: pathlib.Path, destination: pathlib.Path) -> None:
    # sqlite3.Connection's context manager commits/rolls back but does not close; explicit
    # closing avoids leaking two file descriptors for every per-test snapshot restore.
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(destination)) as dst:
        src.backup(dst)


def _restore_pristine_database() -> None:
    """Restore the complete session baseline using SQLite's consistent backup API."""
    _backup_database(_PRISTINE_DB, pathlib.Path(os.environ["OLIVER_DB_PATH"]))


@pytest.fixture(scope="session", autouse=True)
def _corpus_on_disk():
    """Generate the test corpus from the fixture DB once per session, into the temp
    OLIVER_CORPUS_DIR, so tests that read corpus/data directly work without the live corpus."""
    global _PRISTINE_CORPUS_SIG
    from agent import corpus_gen, corpus_read, database, db

    database.initialize()
    with db.connect() as conn:
        _reseed_club(conn)
    corpus_gen.generate()  # DEFAULT_OUT honors OLIVER_CORPUS_DIR
    _PRISTINE_CORPUS_SIG = corpus_read._corpus_signature()
    _backup_database(db.DB_PATH, _PRISTINE_DB)
    yield


@pytest.fixture(autouse=True)
def _frozen_club_clock(monkeypatch):
    """Freeze the club clock so time-dependent meeting logic is deterministic regardless of when
    the suite runs. The fixture world's upcoming meeting is 2026-06-30 18:30 (A World Appears); pin
    "now" to the day before so it's reliably upcoming. Also clears the derived books cache so
    isUpcoming is recomputed under the frozen clock. A test needing a specific instant can re-patch
    agent.clock.club_now."""
    import datetime as _dt
    from zoneinfo import ZoneInfo

    from agent import clock
    from agent import corpus_read as _cr

    frozen = _dt.datetime(2026, 6, 29, 12, 0, tzinfo=ZoneInfo("America/Chicago"))
    monkeypatch.setattr(clock, "club_now", lambda: frozen)
    _cr.invalidate_books()
    yield


@pytest.fixture(autouse=True)
def _no_publish(monkeypatch):
    """Never shell out to npm/gh-pages during tests."""
    from agent import publish

    monkeypatch.setattr(publish, "publish_site", lambda *a, **k: {"deployed": False})


# Keep email tests and dispatch tests offline even when the host .env has a live token.
os.environ["FASTMAIL_JMAP_TOKEN"] = ""


@pytest.fixture
def fresh_db(_corpus_on_disk):
    """Restore an isolated complete DB snapshot; yields the db module.

    Tests that write either operational or ``club_*`` state request this fixture explicitly.
    Pure tests share the immutable session seed and avoid all database reset work.
    """
    global _PRISTINE_CORPUS_SIG
    from agent import corpus_gen, corpus_read
    from agent import db as _db

    _restore_pristine_database()
    yield _db
    _restore_pristine_database()
    # Most DB-writing tests never touch the generated corpus. Restore it only when a write-path
    # test actually changed the configured on-disk generation, preserving order independence
    # without making every isolated test pay for a full generation.
    if corpus_read._corpus_signature() != _PRISTINE_CORPUS_SIG:
        corpus_gen.generate()
        _PRISTINE_CORPUS_SIG = corpus_read._corpus_signature()


@pytest.fixture
def reset_books_cache():
    """Clear the books() module-level cache before and after a test."""
    from agent import corpus_read as cr

    cr.invalidate_books()
    yield
    cr.invalidate_books()
