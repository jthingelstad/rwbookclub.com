"""Shared pytest setup for the Oliver test suite.

Sets `OLIVER_DB_PATH` to a temp file BEFORE any agent module is imported, so
the schema gets created in a scratch location instead of the live `agent/oliver.db`.
Provides a `fresh_db` fixture that truncates all tables between tests.
"""

from __future__ import annotations

import os
import pathlib
import tempfile

import pytest

# Set the env var BEFORE any agent module gets a chance to import db.
_TMP_DIR = pathlib.Path(tempfile.mkdtemp(prefix="oliver-tests-"))
os.environ["OLIVER_DB_PATH"] = str(_TMP_DIR / "test.db")
# Don't push from tests — if something accidentally exercises gitwrite, fail closed.
os.environ.setdefault("OLIVER_GIT_PUSH", "0")
os.environ.setdefault("OLIVER_GIT_DRYRUN", "1")
# Keep /oliver add-book offline in tests — no inline external enrichment (network).
os.environ["OLIVER_ENRICH_ON_WRITE"] = "0"
# Keep email tests and dispatch tests offline even when the host .env has a live token.
os.environ["FASTMAIL_JMAP_TOKEN"] = ""
os.environ["TINYLYTICS_SITE_ID"] = ""
os.environ["TINYLYTICS_SITE_ID_NUMERIC"] = ""
os.environ["TINYLYTICS_API_KEY"] = ""


@pytest.fixture(autouse=True)
def _seed_club_from_corpus():
    """Mirror the corpus into the authoritative club_* tables before each test, so the
    now-DB-backed meeting/ops logic (FKs, member enumeration, meetingId resolution) works.
    Per-test (not session) so a test that mutates club_* (e.g. adds a member) can't leak
    into another. Member ids are minted; books/meetings keep their corpus ids."""
    from agent import clubdb
    from agent import corpus_read as cr
    from agent import db as _db
    clubdb.ensure_schema()
    with _db.connect() as conn:
        # Clear everything that FKs into club_members / club_meetings first, so deleting
        # club rows can't trip a foreign-key constraint from a prior test's leftover rows.
        for t in ("email_opens", "email_tracking", "member_contacts",
                  "reading_statuses", "meeting_attendance", "roll_calls",
                  "mail_message_fts", "mail_messages", "mail_participant_addresses",
                  "mail_participants", "identity_claims", "member_identities"):
            conn.execute(f"DELETE FROM {t}")
        for t in reversed(clubdb.CLUB_TABLES):
            conn.execute(f"DELETE FROM {t}")
        member_id: dict[str, int] = {}
        for i, m in enumerate(cr.members(), start=1):
            member_id[m["slug"]] = i
            conn.execute(
                "INSERT INTO club_members(id, slug, name, is_current) VALUES (?, ?, ?, ?)",
                (i, m["slug"], m["name"], 1 if m.get("isCurrent") else 0),
            )
        for b in cr.books():
            conn.execute(
                "INSERT OR IGNORE INTO club_books(id, slug, title) VALUES (?, ?, ?)",
                (b["bookId"], b["slug"], b["title"]),
            )
            for j, ps in enumerate(b.get("picker") or []):
                if ps in member_id:
                    conn.execute(
                        "INSERT OR IGNORE INTO club_book_pickers(book_id, member_id, ordinal) "
                        "VALUES (?, ?, ?)",
                        (b["bookId"], member_id[ps], j),
                    )
        for mt in cr.meetings():
            conn.execute(
                "INSERT OR IGNORE INTO club_meetings(id, date, start_time, placeholder) "
                "VALUES (?, ?, ?, ?)",
                (mt["meetingId"], mt.get("date"), mt.get("startTime"),
                 1 if mt.get("placeholder") else 0),
            )
            for j, hslug in enumerate(mt.get("host") or []):
                if hslug in member_id:
                    conn.execute(
                        "INSERT OR IGNORE INTO club_meeting_hosts(meeting_id, member_id, ordinal) "
                        "VALUES (?, ?, ?)",
                        (mt["meetingId"], member_id[hslug], j),
                    )
            for j, bslug in enumerate(mt.get("books") or []):
                row = conn.execute("SELECT id FROM club_books WHERE slug = ?", (bslug,)).fetchone()
                if row:
                    conn.execute(
                        "INSERT OR IGNORE INTO club_meeting_books(meeting_id, book_id, ordinal) "
                        "VALUES (?, ?, ?)",
                        (mt["meetingId"], row["id"], j),
                    )
    yield


@pytest.fixture
def fresh_db():
    """Truncate every table; yields the db module."""
    from agent import db as _db
    tables = [
        "memories", "conversations", "channel_summaries", "reminders",
        "usage_log", "notifications_sent", "responses", "feedback",
        "member_identities", "meeting_attendance", "roll_calls", "proposals",
        "inbound_emails",
        "reading_statuses",
        "activity_events",
        "email_opens", "email_tracking", "member_contacts",
        "mail_message_fts", "mail_messages", "mail_threads",
        "identity_claims", "mail_participant_addresses", "mail_participants",
    ]
    with _db.connect() as conn:
        for t in tables:
            conn.execute(f"DELETE FROM {t}")
    yield _db


@pytest.fixture
def reset_books_cache():
    """Clear the books() module-level cache before and after a test."""
    from agent import corpus_read as cr
    cr._books_cache = None
    cr._books_cache_sig = None
    yield
    cr._books_cache = None
    cr._books_cache_sig = None
