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
from datetime import datetime, timedelta, timezone
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

CREATE TABLE IF NOT EXISTS member_identities (
    discord_user_id TEXT PRIMARY KEY,
    member_slug     TEXT NOT NULL,
    linked_by       TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_member_identities_slug ON member_identities(member_slug);

CREATE TABLE IF NOT EXISTS member_emails (
    email       TEXT PRIMARY KEY,
    member_slug TEXT NOT NULL,
    linked_by   TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_member_emails_slug ON member_emails(member_slug);

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

CREATE TABLE IF NOT EXISTS roll_calls (
    meeting_key TEXT PRIMARY KEY,
    channel_id  TEXT,
    message_id  TEXT,
    status      TEXT NOT NULL DEFAULT 'open', -- open | closed
    opened_by   TEXT,
    opened_at   TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at   TEXT
);

CREATE TABLE IF NOT EXISTS meeting_attendance (
    meeting_key        TEXT NOT NULL,
    member_slug        TEXT NOT NULL,
    status             TEXT NOT NULL,          -- yes | no | unsure
    source             TEXT NOT NULL DEFAULT 'button',
    updated_by_user_id TEXT,
    responded_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (meeting_key, member_slug)
);
CREATE INDEX IF NOT EXISTS idx_attendance_meeting ON meeting_attendance(meeting_key);

CREATE TABLE IF NOT EXISTS proposals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL,
    title          TEXT NOT NULL,
    body           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending', -- pending | accepted | dismissed
    channel_id     TEXT,
    source_user_id TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_by    TEXT,
    resolved_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status, created_at);

CREATE TABLE IF NOT EXISTS inbound_emails (
    email_id       TEXT PRIMARY KEY,
    thread_id      TEXT,
    from_email     TEXT,
    subject        TEXT,
    status         TEXT NOT NULL DEFAULT 'processed', -- processing | processed | ignored | failed
    reply_email_id TEXT,
    error          TEXT,
    received_at    TEXT,
    processed_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_inbound_emails_status ON inbound_emails(status, processed_at);

CREATE TABLE IF NOT EXISTS reading_statuses (
    meeting_key  TEXT NOT NULL,
    member_slug  TEXT NOT NULL,
    status       TEXT NOT NULL,
    progress     TEXT,
    page         INTEGER,
    percent      INTEGER,
    source       TEXT,
    updated_by   TEXT,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (meeting_key, member_slug)
);
CREATE INDEX IF NOT EXISTS idx_reading_statuses_meeting ON reading_statuses(meeting_key, updated_at);

CREATE TABLE IF NOT EXISTS activity_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT,
    status      TEXT NOT NULL DEFAULT 'pending', -- pending | posted | dead
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    next_attempt_at TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    posted_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_activity_events_status ON activity_events(status, id);

CREATE TABLE IF NOT EXISTS member_contacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_key TEXT NOT NULL,
    member_slug TEXT NOT NULL,
    kind        TEXT NOT NULL, -- roll_call | reading_checkin | email_reply
    surface     TEXT NOT NULL, -- discord | email
    direction   TEXT NOT NULL, -- inbound | outbound
    status      TEXT NOT NULL, -- sent | received | skipped | failed
    subject     TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_member_contacts_meeting ON member_contacts(meeting_key, member_slug, created_at);

CREATE TABLE IF NOT EXISTS email_tracking (
    token       TEXT PRIMARY KEY,
    contact_id  INTEGER,
    meeting_key TEXT,
    member_slug TEXT,
    kind        TEXT,
    subject     TEXT,
    email_id    TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(contact_id) REFERENCES member_contacts(id)
);
CREATE INDEX IF NOT EXISTS idx_email_tracking_meeting ON email_tracking(meeting_key, member_slug);

CREATE TABLE IF NOT EXISTS email_opens (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token       TEXT NOT NULL,
    opened_at   TEXT NOT NULL DEFAULT (datetime('now')),
    remote_addr TEXT,
    user_agent  TEXT,
    FOREIGN KEY(token) REFERENCES email_tracking(token)
);
CREATE INDEX IF NOT EXISTS idx_email_opens_token ON email_opens(token, opened_at);
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

    roll_call_cols = _columns(conn, "roll_calls")
    roll_call_additions = {
        "channel_id": "TEXT",
        "message_id": "TEXT",
        "status": "TEXT NOT NULL DEFAULT 'open'",
        "opened_by": "TEXT",
        "opened_at": "TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00'",
        "closed_at": "TEXT",
    }
    for col, spec in roll_call_additions.items():
        if col not in roll_call_cols:
            conn.execute(f"ALTER TABLE roll_calls ADD COLUMN {col} {spec}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_roll_calls_status ON roll_calls(status, opened_at)"
    )

    activity_cols = _columns(conn, "activity_events")
    activity_additions = {
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "last_error": "TEXT",
        "next_attempt_at": "TEXT",
    }
    for col, spec in activity_additions.items():
        if col not in activity_cols:
            conn.execute(f"ALTER TABLE activity_events ADD COLUMN {col} {spec}")


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


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def link_member_email(email: str, member_slug: str, *, linked_by: str | None = None) -> None:
    email = _normalize_email(email)
    if not email or "@" not in email:
        raise ValueError("email must look like an email address")
    with connect() as conn:
        conn.execute(
            "INSERT INTO member_emails (email, member_slug, linked_by, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(email) DO UPDATE SET "
            "member_slug=excluded.member_slug, linked_by=excluded.linked_by, "
            "updated_at=excluded.updated_at",
            (email, member_slug, linked_by, _now()),
        )


def member_slug_for_email(email: str | None) -> str | None:
    if not email:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT member_slug FROM member_emails WHERE email = ?",
            (_normalize_email(email),),
        ).fetchone()
    return row["member_slug"] if row else None


def email_for_member(member_slug: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT email, member_slug, linked_by, created_at, updated_at "
            "FROM member_emails WHERE member_slug = ? ORDER BY updated_at DESC LIMIT 1",
            (member_slug,),
        ).fetchone()
    return dict(row) if row else None


def list_member_emails() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT email, member_slug, linked_by, created_at, updated_at "
            "FROM member_emails ORDER BY member_slug, email"
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


def recent_messages(channel_id: str, limit: int = 12) -> list[dict]:
    """Recent Oliver-visible conversation turns for a channel, newest last."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, role, speaker, content, created_at FROM conversations "
            "WHERE channel_id = ? ORDER BY id DESC LIMIT ?",
            (channel_id, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def search_conversations(query: str, *, limit: int = 12,
                         channel_ids: list[str] | None = None) -> list[dict]:
    """Keyword search over logged turns across ALL channels, newest first.

    Splits the query into whitespace terms; a row must contain every term
    (AND match). No channel filter by default, so this spans every channel
    Oliver has logged. The simple LIKE backend is swappable for FTS5/embeddings
    later without changing callers.
    """
    terms = [t for t in query.split() if t]
    if not terms:
        return []
    sql = (
        "SELECT id, channel_id, role, speaker, content, created_at FROM conversations "
        "WHERE " + " AND ".join("content LIKE ?" for _ in terms)
    )
    args: list = [f"%{t}%" for t in terms]
    if channel_ids:
        sql += f" AND channel_id IN ({','.join('?' for _ in channel_ids)})"
        args += channel_ids
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


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


# ── Meeting roll call + attendance ──────────────────────────────────────────
def upsert_roll_call(*, meeting_key: str, channel_id: str | None = None,
                     message_id: str | None = None, opened_by: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO roll_calls (meeting_key, channel_id, message_id, opened_by, status) "
            "VALUES (?, ?, ?, ?, 'open') "
            "ON CONFLICT(meeting_key) DO UPDATE SET "
            "channel_id=COALESCE(excluded.channel_id, roll_calls.channel_id), "
            "message_id=COALESCE(excluded.message_id, roll_calls.message_id), "
            "opened_by=COALESCE(excluded.opened_by, roll_calls.opened_by), "
            "status='open', closed_at=NULL",
            (meeting_key, channel_id, message_id, opened_by),
        )


def get_roll_call(meeting_key: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM roll_calls WHERE meeting_key = ?",
            (meeting_key,),
        ).fetchone()
    return dict(row) if row else None


def close_roll_call(meeting_key: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE roll_calls SET status = 'closed', closed_at = ? "
            "WHERE meeting_key = ? AND status != 'closed'",
            (_now(), meeting_key),
        )
        return cur.rowcount > 0


def set_attendance(*, meeting_key: str, member_slug: str, status: str,
                   updated_by_user_id: str | None = None, source: str = "button") -> None:
    if status not in {"yes", "no", "unsure"}:
        raise ValueError("attendance status must be yes, no, or unsure")
    with connect() as conn:
        conn.execute(
            "INSERT INTO meeting_attendance "
            "(meeting_key, member_slug, status, source, updated_by_user_id, responded_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(meeting_key, member_slug) DO UPDATE SET "
            "status=excluded.status, source=excluded.source, "
            "updated_by_user_id=excluded.updated_by_user_id, responded_at=excluded.responded_at",
            (meeting_key, member_slug, status, source, updated_by_user_id, _now()),
        )


def attendance_for_meeting(meeting_key: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM meeting_attendance WHERE meeting_key = ? ORDER BY member_slug",
            (meeting_key,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Oliver action proposals ─────────────────────────────────────────────────
def add_proposal(*, kind: str, title: str, body: str, channel_id: str | None = None,
                 source_user_id: str | None = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO proposals (kind, title, body, channel_id, source_user_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (kind, title, body, channel_id, source_user_id),
        )
        return cur.lastrowid


def list_proposals(*, status: str = "pending", limit: int = 10) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM proposals WHERE status = ? ORDER BY id DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def resolve_proposal(proposal_id: int, status: str, *, resolved_by: str | None = None) -> bool:
    if status not in {"accepted", "dismissed"}:
        raise ValueError("proposal status must be accepted or dismissed")
    with connect() as conn:
        cur = conn.execute(
            "UPDATE proposals SET status = ?, resolved_by = ?, resolved_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (status, resolved_by, _now(), proposal_id),
        )
        return cur.rowcount > 0


# ── Inbound email dedupe ─────────────────────────────────────────────────────
def email_processed(email_id: str) -> bool:
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM inbound_emails WHERE email_id = ? AND status IN ('processed', 'ignored')",
            (email_id,),
        ).fetchone() is not None


def mark_email_processing(*, email_id: str, thread_id: str | None = None,
                          from_email: str | None = None, subject: str | None = None,
                          received_at: str | None = None,
                          stale_after_seconds: int = 1800) -> bool:
    """Claim an inbound email for processing. Failed emails may be retried."""
    now = _now()
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO inbound_emails "
            "(email_id, thread_id, from_email, subject, status, received_at, processed_at) "
            "VALUES (?, ?, ?, ?, 'processing', ?, ?)",
            (email_id, thread_id, from_email, subject, received_at, now),
        )
        if cur.rowcount > 0:
            return True
        cur = conn.execute(
            "UPDATE inbound_emails SET thread_id = COALESCE(?, thread_id), "
            "from_email = COALESCE(?, from_email), subject = COALESCE(?, subject), "
            "received_at = COALESCE(?, received_at), status = 'processing', "
            "error = NULL, processed_at = ? "
            "WHERE email_id = ? AND (status = 'failed' OR "
            "(status = 'processing' AND datetime(replace(substr(processed_at, 1, 19), 'T', ' ')) <= datetime(?)))",
            (thread_id, from_email, subject, received_at, now, email_id, cutoff),
        )
        return cur.rowcount > 0


def mark_email_processed(email_id: str, *, reply_email_id: str | None = None,
                         status: str = "processed", error: str | None = None) -> None:
    if status not in {"processed", "ignored", "failed"}:
        raise ValueError("email status must be processed, ignored, or failed")
    with connect() as conn:
        conn.execute(
            "UPDATE inbound_emails SET status = ?, reply_email_id = ?, error = ?, "
            "processed_at = ? WHERE email_id = ?",
            (status, reply_email_id, error, _now(), email_id),
        )


# ── Reading progress ─────────────────────────────────────────────────────────
READING_STATUSES = {"not_started", "started", "on_track", "behind", "finished", "paused"}


def set_reading_status(*, meeting_key: str, member_slug: str, status: str,
                       progress: str | None = None, page: int | None = None,
                       percent: int | None = None, source: str | None = None,
                       updated_by: str | None = None) -> None:
    if status not in READING_STATUSES:
        raise ValueError(f"reading status must be one of {', '.join(sorted(READING_STATUSES))}")
    if page is not None and page < 0:
        raise ValueError("page must be non-negative")
    if percent is not None and not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    with connect() as conn:
        conn.execute(
            "INSERT INTO reading_statuses "
            "(meeting_key, member_slug, status, progress, page, percent, source, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(meeting_key, member_slug) DO UPDATE SET "
            "status=excluded.status, progress=excluded.progress, page=excluded.page, "
            "percent=excluded.percent, source=excluded.source, updated_by=excluded.updated_by, "
            "updated_at=excluded.updated_at",
            (meeting_key, member_slug, status, progress, page, percent, source, updated_by, _now()),
        )


def reading_status_for_meeting(meeting_key: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reading_statuses WHERE meeting_key = ? ORDER BY member_slug",
            (meeting_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def reading_status_for_member(meeting_key: str, member_slug: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM reading_statuses WHERE meeting_key = ? AND member_slug = ?",
            (meeting_key, member_slug),
        ).fetchone()
    return dict(row) if row else None


# ── Activity log bridge to #oliver-log ───────────────────────────────────────
def add_activity(kind: str, title: str, body: str | None = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO activity_events (kind, title, body) VALUES (?, ?, ?)",
            (kind, title, body),
        )
        return cur.lastrowid


def pending_activity(limit: int = 10) -> list[dict]:
    now = _now()
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, kind, title, body, attempts, last_error, created_at FROM activity_events "
            "WHERE status = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
            "ORDER BY id LIMIT ?",
            (now, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_activity_posted(activity_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE activity_events SET status = 'posted', posted_at = ? WHERE id = ?",
            (_now(), activity_id),
        )


def mark_activity_failed(activity_id: int, error: str, *, max_attempts: int = 5,
                         retry_delay_seconds: int = 60) -> None:
    with connect() as conn:
        row = conn.execute(
            "SELECT attempts FROM activity_events WHERE id = ?",
            (activity_id,),
        ).fetchone()
        if not row:
            return
        attempts = int(row["attempts"] or 0) + 1
        status = "dead" if attempts >= max_attempts else "pending"
        next_attempt_at = None
        if status == "pending":
            delay = retry_delay_seconds * attempts
            next_attempt_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        conn.execute(
            "UPDATE activity_events SET status = ?, attempts = ?, last_error = ?, "
            "next_attempt_at = ? WHERE id = ?",
            (status, attempts, error[:500], next_attempt_at, activity_id),
        )


# ── Member contact/campaign state ────────────────────────────────────────────
def add_member_contact(*, meeting_key: str, member_slug: str, kind: str,
                       surface: str, direction: str, status: str,
                       subject: str | None = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO member_contacts "
            "(meeting_key, member_slug, kind, surface, direction, status, subject) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (meeting_key, member_slug, kind, surface, direction, status, subject),
        )
        return cur.lastrowid


def update_member_contact_status(contact_id: int, status: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE member_contacts SET status = ? WHERE id = ?",
            (status, contact_id),
        )


def member_contacts_for_meeting(meeting_key: str, *, limit: int = 200) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM member_contacts WHERE meeting_key = ? "
            "ORDER BY datetime(created_at) DESC, id DESC LIMIT ?",
            (meeting_key, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def add_email_tracking(*, token: str, contact_id: int | None = None,
                       meeting_key: str | None = None, member_slug: str | None = None,
                       kind: str | None = None, subject: str | None = None,
                       email_id: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO email_tracking "
            "(token, contact_id, meeting_key, member_slug, kind, subject, email_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (token, contact_id, meeting_key, member_slug, kind, subject, email_id),
        )


def mark_email_tracking_sent(token: str, email_id: str | None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE email_tracking SET email_id = ? WHERE token = ?",
            (email_id, token),
        )


def record_email_open(token: str, *, remote_addr: str | None = None,
                      user_agent: str | None = None) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM email_tracking WHERE token = ?",
            (token,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "INSERT INTO email_opens (token, remote_addr, user_agent) VALUES (?, ?, ?)",
            (token, remote_addr, user_agent),
        )
        if row["contact_id"]:
            conn.execute(
                "UPDATE member_contacts SET status = 'opened' WHERE id = ?",
                (row["contact_id"],),
            )
        return dict(row)


def email_open_summary(meeting_key: str) -> dict[str, dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT et.member_slug, MAX(eo.opened_at) AS opened_at, COUNT(eo.id) AS open_count "
            "FROM email_tracking et JOIN email_opens eo ON eo.token = et.token "
            "WHERE et.meeting_key = ? AND et.member_slug IS NOT NULL "
            "GROUP BY et.member_slug",
            (meeting_key,),
        ).fetchall()
    return {r["member_slug"]: dict(r) for r in rows}


def tracked_emails_without_open(*, limit: int = 200) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT et.* FROM email_tracking et "
            "LEFT JOIN email_opens eo ON eo.token = et.token "
            "WHERE eo.id IS NULL "
            "ORDER BY et.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
