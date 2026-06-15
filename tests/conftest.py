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
# Keep email tests and dispatch tests offline even when the host .env has a live token.
os.environ["FASTMAIL_JMAP_TOKEN"] = ""
os.environ["TINYLYTICS_SITE_ID"] = ""
os.environ["TINYLYTICS_SITE_ID_NUMERIC"] = ""
os.environ["TINYLYTICS_API_KEY"] = ""


@pytest.fixture
def fresh_db():
    """Truncate every table; yields the db module."""
    from agent import db as _db
    tables = [
        "memories", "conversations", "channel_summaries", "reminders",
        "usage_log", "notifications_sent", "responses", "feedback",
        "member_identities", "meeting_attendance", "roll_calls", "proposals",
        "inbound_emails",
        "member_emails", "reading_statuses",
        "activity_events",
        "email_opens", "email_tracking", "member_contacts",
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
