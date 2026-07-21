"""Ordered application-migration ledger and legacy-upgrade coverage."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agent import database


def _ledger(db) -> list[tuple[int, str, str]]:
    with db.connect() as conn:
        return [
            (row["version"], row["name"], row["applied_at"])
            for row in conn.execute(
                "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
            )
        ]


def test_fresh_database_has_complete_ordered_ledger(fresh_db):
    rows = _ledger(fresh_db)

    assert [(version, name) for version, name, _ in rows] == [
        (version, name) for version, name, _ in database.MIGRATIONS
    ]
    assert all(applied_at for _, _, applied_at in rows)


def test_run_migrations_is_idempotent(fresh_db):
    before = _ledger(fresh_db)

    database.run_migrations()

    assert _ledger(fresh_db) == before


def test_author_validation_migration_upgrades_existing_sidecar():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE club_author_enrichment (author_id INTEGER PRIMARY KEY)")

    database._add_author_enrichment_validation(conn)
    database._add_author_enrichment_validation(conn)
    conn.execute("INSERT INTO club_author_enrichment(author_id) VALUES (1)")
    row = conn.execute("SELECT * FROM club_author_enrichment").fetchone()

    assert row["validation_status"] == "unvalidated"
    assert row["validation_warnings_json"] == "[]"
    conn.close()


def test_private_pronoun_migration_seeds_current_members_once_without_a_future_default():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE club_members ("
        "id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL, "
        "is_current INTEGER NOT NULL DEFAULT 0);"
        "CREATE TABLE member_preferences ("
        "member_id INTEGER PRIMARY KEY REFERENCES club_members(id), "
        "pronouns TEXT NOT NULL, source TEXT NOT NULL, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT NOT NULL);"
        "INSERT INTO club_members VALUES (1, 'jamie', 'Jamie', 1);"
        "INSERT INTO club_members VALUES (2, 'oliver', 'Oliver', 1);"
        "INSERT INTO club_members VALUES (3, 'former', 'Former', 0);"
        "INSERT INTO member_preferences(member_id, pronouns, source, updated_at) "
        "VALUES (1, 'they/them', 'member', 'earlier');"
    )

    database._seed_private_member_pronouns(conn)

    rows = [
        tuple(row)
        for row in conn.execute(
            "SELECT m.slug, p.pronouns, p.source FROM member_preferences p "
            "JOIN club_members m ON m.id = p.member_id ORDER BY m.id"
        )
    ]
    assert rows == [
        ("jamie", "they/them", "member"),  # never overwrite an explicit value
        ("oliver", "he/him", "admin_seed"),
    ]

    # The migration is a one-time backfill, not a schema default for later members.
    conn.execute("INSERT INTO club_members VALUES (4, 'future', 'Future', 1)")
    assert conn.execute("SELECT 1 FROM member_preferences WHERE member_id = 4").fetchone() is None
    conn.close()


def test_registered_pronoun_migration_backfills_every_current_member_including_oliver(fresh_db):
    with fresh_db.connect() as conn:
        conn.execute("DELETE FROM member_preferences")
        conn.execute("DELETE FROM schema_migrations WHERE version = 11")

    database.run_migrations()

    with fresh_db.connect() as conn:
        rows = conn.execute(
            "SELECT m.slug, p.pronouns FROM club_members m "
            "LEFT JOIN member_preferences p ON p.member_id = m.id "
            "WHERE m.is_current = 1 ORDER BY m.slug"
        ).fetchall()
    assert rows
    assert "oliver" in {row["slug"] for row in rows}
    assert all(row["pronouns"] == "he/him" for row in rows)


def test_runner_applies_each_migration_once_in_order(monkeypatch, fresh_db):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, applied_at TEXT NOT NULL)"
    )
    calls: list[int] = []

    def migration(version: int):
        return lambda _conn: calls.append(version)

    monkeypatch.setattr(
        database,
        "MIGRATIONS",
        ((1, "first", migration(1)), (2, "second", migration(2))),
    )

    database._run_migrations(conn)
    database._run_migrations(conn)

    assert calls == [1, 2]
    assert [
        tuple(row)
        for row in conn.execute("SELECT version, name FROM schema_migrations ORDER BY version")
    ] == [(1, "first"), (2, "second")]
    conn.close()


def test_runner_rejects_a_gapped_ledger(fresh_db):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, applied_at TEXT NOT NULL);"
        "INSERT INTO schema_migrations VALUES (1, 'additive_runtime_columns', 'now');"
        "INSERT INTO schema_migrations VALUES (3, 'unified_member_identities', 'now');"
    )

    with pytest.raises(RuntimeError, match="ledger has a gap"):
        database._run_migrations(conn)
    conn.close()


def test_legacy_email_tracking_upgrade_is_recorded(fresh_db):
    with fresh_db.connect() as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version >= 7")
        conn.executescript(
            "CREATE TABLE email_tracking (token TEXT PRIMARY KEY);"
            "CREATE TABLE email_opens (id INTEGER PRIMARY KEY, token TEXT);"
        )

    database.run_migrations()

    with fresh_db.connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'email_%'"
            )
        }
    assert tables == set()
    assert [(version, name) for version, name, _ in _ledger(fresh_db)][-4:] == [
        (8, "unified_meeting_events"),
        (9, "legacy_club_schema"),
        (10, "author_enrichment_validation"),
        (11, "private_member_pronouns"),
    ]


def test_importing_database_modules_is_inert(tmp_path):
    target = tmp_path / "not-created-by-import.db"
    env = os.environ.copy()
    env["OLIVER_DB_PATH"] = str(target)
    subprocess.run(
        [sys.executable, "-c", "from agent import db, clubdb"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=True,
    )
    assert not target.exists()
