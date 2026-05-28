"""Oliver's private memory & state — SQLite (class B).

Holds what doesn't belong in the public Git corpus: durable notes Oliver learns,
per-channel conversation history + rolling summaries, reminders, and usage logs.
Gitignored, local to wherever Oliver runs; backup is a deployment concern.

Schema is created idempotently on import (CREATE TABLE IF NOT EXISTS) — no
migration ordering to remember. Each helper opens a short-lived connection so the
module is safe to call from the bot's worker threads.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("OLIVER_DB_PATH") or Path(__file__).resolve().parent / "oliver.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT NOT NULL DEFAULT 'general',   -- member | club | general
    subject    TEXT,                              -- e.g. a member slug
    note       TEXT NOT NULL,
    source     TEXT,                              -- who/what recorded it
    source_user_id TEXT,
    source_message_id TEXT,
    confidence REAL NOT NULL DEFAULT 1.0,
    status     TEXT NOT NULL DEFAULT 'active',     -- active | deleted
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_memories_subject ON memories(subject);
CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);

CREATE TABLE IF NOT EXISTS member_identities (
    discord_user_id TEXT PRIMARY KEY,
    member_slug     TEXT NOT NULL,
    linked_by       TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_member_identities_slug ON member_identities(member_slug);

CREATE TABLE IF NOT EXISTS conversations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    role       TEXT NOT NULL,                     -- user | assistant
    speaker    TEXT,                              -- display name (for user turns)
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_conv_channel ON conversations(channel_id, id);

CREATE TABLE IF NOT EXISTS channel_summaries (
    channel_id TEXT PRIMARY KEY,
    summary    TEXT NOT NULL,
    last_id    INTEGER NOT NULL DEFAULT 0,        -- highest conversations.id folded in
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reminders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    due_at     TEXT NOT NULL,
    channel_id TEXT,
    text       TEXT NOT NULL,
    created_by TEXT,
    fired_at   TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS usage_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id      TEXT,
    model           TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cache_read      INTEGER,
    cache_creation  INTEGER,
    rounds          INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Dedup for the proactive scheduler: each notification key is posted at most once.
CREATE TABLE IF NOT EXISTS notifications_sent (
    key     TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Each reply Oliver posts to Discord — keyed by Discord message id so reaction
-- handlers can cheaply check "is this one of mine?" without fetching the message.
CREATE TABLE IF NOT EXISTS responses (
    message_id  TEXT PRIMARY KEY,
    channel_id  TEXT NOT NULL,
    speaker     TEXT,
    question    TEXT,
    reply       TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_responses_channel ON responses(channel_id, created_at);

-- 👍/👎 reactions members give to Oliver's replies. One row per reaction event;
-- analysis can group by (user_id, message_id) and take the latest.
CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  TEXT NOT NULL,
    channel_id  TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    user_name   TEXT,
    reaction    TEXT NOT NULL,           -- 'up' or 'down'
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_feedback_message ON feedback(message_id);
CREATE INDEX IF NOT EXISTS idx_feedback_reaction ON feedback(reaction, created_at);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """Small additive migrations for long-lived Oliver SQLite files."""
    memory_cols = _columns(conn, "memories")
    additions = {
        "source_user_id": "TEXT",
        "source_message_id": "TEXT",
        "confidence": "REAL NOT NULL DEFAULT 1.0",
        "status": "TEXT NOT NULL DEFAULT 'active'",
    }
    for col, spec in additions.items():
        if col not in memory_cols:
            conn.execute(f"ALTER TABLE memories ADD COLUMN {col} {spec}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status)")


def _ensure_schema() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)


_ensure_schema()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Memories ─────────────────────────────────────────────────────────────────
def add_memory(note: str, *, scope: str = "general", subject: str | None = None,
               source: str | None = None, source_user_id: str | None = None,
               source_message_id: str | None = None, confidence: float = 1.0) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO memories "
            "(scope, subject, note, source, source_user_id, source_message_id, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (scope, subject, note, source, source_user_id, source_message_id, confidence),
        )
        return cur.lastrowid


def get_memories(*, subject: str | None = None, scope: str | None = None,
                 query: str | None = None, limit: int = 50) -> list[dict]:
    sql = (
        "SELECT id, scope, subject, note, source, source_user_id, source_message_id, "
        "confidence, created_at FROM memories WHERE status = 'active'"
    )
    args: list = []
    if subject:
        sql += " AND subject = ?"; args.append(subject)
    if scope:
        sql += " AND scope = ?"; args.append(scope)
    if query:
        sql += " AND note LIKE ?"; args.append(f"%{query}%")
    sql += " ORDER BY id DESC LIMIT ?"; args.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def update_memory(memory_id: int, note: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE memories SET note = ? WHERE id = ? AND status = 'active'",
            (note, memory_id),
        )
        return cur.rowcount > 0


def delete_memory(memory_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE memories SET status = 'deleted' WHERE id = ? AND status = 'active'",
            (memory_id,),
        )
        return cur.rowcount > 0


# ── Discord identity map ─────────────────────────────────────────────────────
def link_member_identity(discord_user_id: str, member_slug: str, *, linked_by: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO member_identities (discord_user_id, member_slug, linked_by, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(discord_user_id) DO UPDATE SET "
            "member_slug=excluded.member_slug, linked_by=excluded.linked_by, "
            "updated_at=excluded.updated_at",
            (discord_user_id, member_slug, linked_by, _now()),
        )


def member_slug_for_user(discord_user_id: str | None) -> str | None:
    if not discord_user_id:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT member_slug FROM member_identities WHERE discord_user_id = ?",
            (discord_user_id,),
        ).fetchone()
    return row["member_slug"] if row else None


def identity_for_member(member_slug: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT discord_user_id, member_slug, linked_by, created_at, updated_at "
            "FROM member_identities WHERE member_slug = ? ORDER BY updated_at DESC LIMIT 1",
            (member_slug,),
        ).fetchone()
    return dict(row) if row else None


def list_member_identities() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT discord_user_id, member_slug, linked_by, created_at, updated_at "
            "FROM member_identities ORDER BY member_slug"
        ).fetchall()
    return [dict(r) for r in rows]


# ── Conversations + rolling summary ──────────────────────────────────────────
def log_message(channel_id: str, role: str, content: str, speaker: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO conversations (channel_id, role, speaker, content) VALUES (?, ?, ?, ?)",
            (channel_id, role, speaker, content),
        )


def get_summary(channel_id: str) -> tuple[str | None, int]:
    with connect() as conn:
        row = conn.execute(
            "SELECT summary, last_id FROM channel_summaries WHERE channel_id = ?", (channel_id,)
        ).fetchone()
    return (row["summary"], row["last_id"]) if row else (None, 0)


def set_summary(channel_id: str, summary: str, last_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO channel_summaries (channel_id, summary, last_id, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(channel_id) DO UPDATE SET "
            "summary=excluded.summary, last_id=excluded.last_id, updated_at=excluded.updated_at",
            (channel_id, summary, last_id, _now()),
        )


def messages_after(channel_id: str, after_id: int, limit: int = 200) -> list[dict]:
    """Conversation turns with id > after_id, oldest first."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, role, speaker, content FROM conversations "
            "WHERE channel_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
            (channel_id, after_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Reminders (Phase 4 scheduler fires these) ────────────────────────────────
def add_reminder(due_at: str, text: str, *, channel_id: str | None = None,
                 created_by: str | None = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO reminders (due_at, channel_id, text, created_by) VALUES (?, ?, ?, ?)",
            (due_at, channel_id, text, created_by),
        )
        return cur.lastrowid


def due_reminders(now_iso: str | None = None) -> list[dict]:
    now_iso = now_iso or _now()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reminders WHERE fired_at IS NULL AND due_at <= ? ORDER BY due_at",
            (now_iso,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_reminder_fired(reminder_id: int) -> None:
    """Stamp a reminder as fired so it won't surface again on the next tick."""
    with connect() as conn:
        conn.execute(
            "UPDATE reminders SET fired_at = ? WHERE id = ?",
            (_now(), reminder_id),
        )


# ── Usage / cost ─────────────────────────────────────────────────────────────
def log_usage(channel_id: str | None, model: str, *, input_tokens: int, output_tokens: int,
              cache_read: int, cache_creation: int, rounds: int) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO usage_log (channel_id, model, input_tokens, output_tokens, "
            "cache_read, cache_creation, rounds) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (channel_id, model, input_tokens, output_tokens, cache_read, cache_creation, rounds),
        )


# ── Proactive-notification dedup ─────────────────────────────────────────────
def sent_keys() -> set[str]:
    with connect() as conn:
        return {r["key"] for r in conn.execute("SELECT key FROM notifications_sent")}


def mark_sent(key: str) -> None:
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO notifications_sent (key) VALUES (?)", (key,))


# ── Response logging + 👍/👎 feedback ───────────────────────────────────────
def log_response(*, message_id: str, channel_id: str, speaker: str | None,
                 question: str, reply: str) -> None:
    """Record a reply Oliver sent so we can join feedback back to its question."""
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO responses "
            "(message_id, channel_id, speaker, question, reply) VALUES (?, ?, ?, ?, ?)",
            (message_id, channel_id, speaker, question, reply),
        )


def is_oliver_message(message_id: str) -> bool:
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM responses WHERE message_id = ?", (message_id,)
        ).fetchone() is not None


def add_feedback(*, message_id: str, channel_id: str, user_id: str,
                 user_name: str | None, reaction: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO feedback (message_id, channel_id, user_id, user_name, reaction) "
            "VALUES (?, ?, ?, ?, ?)",
            (message_id, channel_id, user_id, user_name, reaction),
        )


def feedback_stats() -> dict:
    """Counts + the 5 most recent downvotes (with the question that triggered them)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT reaction, COUNT(*) c FROM feedback GROUP BY reaction"
        ).fetchall()
        counts = {r["reaction"]: r["c"] for r in rows}
        recent_down = conn.execute(
            "SELECT f.user_name, f.created_at, r.question "
            "FROM feedback f LEFT JOIN responses r ON r.message_id = f.message_id "
            "WHERE f.reaction = 'down' ORDER BY f.id DESC LIMIT 5"
        ).fetchall()
        recent_up = conn.execute(
            "SELECT f.user_name, f.created_at, r.question "
            "FROM feedback f LEFT JOIN responses r ON r.message_id = f.message_id "
            "WHERE f.reaction = 'up' ORDER BY f.id DESC LIMIT 5"
        ).fetchall()
    return {
        "up": counts.get("up", 0),
        "down": counts.get("down", 0),
        "total": sum(counts.values()),
        "recent_down": [dict(r) for r in recent_down],
        "recent_up": [dict(r) for r in recent_up],
    }
