"""Explicit SQLite bootstrap and the single ordered application-migration ledger.

Importing :mod:`agent.db` and :mod:`agent.clubdb` is deliberately inert. Runtime and CLI
composition roots call :func:`initialize` before using either repository surface, making schema
creation and migration a visible lifecycle step instead of a side effect of importing a helper.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from agent import clubdb, db, security


def _add_author_enrichment_validation(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(club_author_enrichment)")}
    if "validation_status" not in columns:
        conn.execute(
            "ALTER TABLE club_author_enrichment ADD COLUMN validation_status TEXT "
            "NOT NULL DEFAULT 'unvalidated' CHECK (validation_status IN "
            "('unvalidated', 'accepted', 'partial'))"
        )
    if "validation_warnings_json" not in columns:
        conn.execute(
            "ALTER TABLE club_author_enrichment ADD COLUMN validation_warnings_json TEXT "
            "NOT NULL DEFAULT '[]'"
        )


MIGRATIONS = (
    *db.RUNTIME_MIGRATIONS,
    (9, "legacy_club_schema", clubdb.migrate_legacy_club_schema),
    (10, "author_enrichment_validation", _add_author_enrichment_validation),
)


def _run_migrations(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()
    applied = {int(row["version"]): row["name"] for row in rows}
    known_versions = {version for version, _, _ in MIGRATIONS}
    unknown = sorted(set(applied) - known_versions)
    if unknown:
        raise RuntimeError(f"database has migrations newer than this code: {unknown}")
    if applied:
        expected = list(range(1, max(applied) + 1))
        if sorted(applied) != expected:
            raise RuntimeError(
                f"database migration ledger has a gap: {sorted(applied)}, expected {expected}"
            )

    for version, name, migration in MIGRATIONS:
        if version in applied:
            if applied[version] != name:
                raise RuntimeError(
                    f"migration {version} is recorded as {applied[version]!r}, expected {name!r}"
                )
            continue
        migration(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (version, name, datetime.now(timezone.utc).isoformat()),
        )


def run_migrations() -> None:
    """Apply pending migrations after both declarative schemas have been installed."""
    with db.connect() as conn:
        _run_migrations(conn)
        db.ensure_member_indexes(conn)


def initialize() -> None:
    """Create the current schemas and apply every pending migration for ``db.DB_PATH``."""
    security.set_private_umask()
    db.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db.connect() as conn:
        conn.executescript(db.RUNTIME_SCHEMA)
        conn.executescript(clubdb.CLUB_SCHEMA)
        _run_migrations(conn)
        db.ensure_member_indexes(conn)
    security.secure_database_files(db.DB_PATH)
