"""Shared read-only SQLite connection for external evaluator/reporting scripts.

Evaluator code must never initialize, migrate, or otherwise mutate Oliver's live
database merely by inspecting it. Keep that guarantee at the connection boundary.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def configured_db_path(path: str | Path | None = None) -> Path:
    """Resolve an explicit or configured Oliver database path without opening it."""
    load_dotenv(ROOT / ".env", override=False)
    selected = path or os.environ.get("OLIVER_DB_PATH") or ROOT / "agent" / "oliver.db"
    return Path(selected).expanduser().resolve()


def connect_read_only(path: str | Path | None = None) -> sqlite3.Connection:
    """Open Oliver SQLite with URI read-only mode and SQLite's query-only guard."""
    db_path = configured_db_path(path)
    if not db_path.is_file():
        raise FileNotFoundError(f"Oliver database does not exist: {db_path}")
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn
