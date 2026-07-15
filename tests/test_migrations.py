"""Ordered application-migration ledger and legacy-upgrade coverage."""

from __future__ import annotations

import sqlite3

import pytest


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
        (version, name) for version, name, _ in fresh_db._MIGRATIONS
    ]
    assert all(applied_at for _, _, applied_at in rows)


def test_run_migrations_is_idempotent(fresh_db):
    before = _ledger(fresh_db)

    fresh_db.run_migrations()

    assert _ledger(fresh_db) == before


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
        fresh_db,
        "_MIGRATIONS",
        ((1, "first", migration(1)), (2, "second", migration(2))),
    )
    monkeypatch.setattr(fresh_db, "_migration_ready", lambda _conn, _version: True)

    fresh_db._run_migrations(conn)
    fresh_db._run_migrations(conn)

    assert calls == [1, 2]
    assert [tuple(row) for row in conn.execute(
        "SELECT version, name FROM schema_migrations ORDER BY version"
    )] == [(1, "first"), (2, "second")]
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
        fresh_db._run_migrations(conn)
    conn.close()


def test_legacy_email_tracking_upgrade_is_recorded(fresh_db):
    with fresh_db.connect() as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version >= 7")
        conn.executescript(
            "CREATE TABLE email_tracking (token TEXT PRIMARY KEY);"
            "CREATE TABLE email_opens (id INTEGER PRIMARY KEY, token TEXT);"
        )

    fresh_db.run_migrations()

    with fresh_db.connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'email_%'"
            )
        }
    assert tables == set()
    assert [(version, name) for version, name, _ in _ledger(fresh_db)][-2:] == [
        (7, "drop_email_open_tracking"),
        (8, "unified_meeting_events"),
    ]
