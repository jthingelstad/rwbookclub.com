"""Oliver's private memory & state — SQLite (class B).

Holds what doesn't belong in the club record or the generated corpus: durable notes Oliver learns,
per-channel conversation history + rolling summaries, reminders, and usage logs.
Gitignored, local to wherever Oliver runs; backup is a deployment concern.

Schema is created idempotently on import (CREATE TABLE IF NOT EXISTS) — no
migration ordering to remember. Each helper opens a short-lived connection so the
module is safe to call from the bot's worker threads.
"""

from __future__ import annotations

import os
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

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

-- One identity map for every way we recognize / reach a member: Discord ids, email
-- addresses, and phone numbers (the single handle store). Keyed by member_id FK — slug is
-- never stored here. club_members is the person; member_identities holds their handles.
CREATE TABLE IF NOT EXISTS member_identities (
    surface     TEXT NOT NULL,               -- 'discord' | 'email' | 'sms'
    identifier  TEXT NOT NULL,               -- discord user id | normalized email | E.164-ish phone
    member_id   INTEGER NOT NULL REFERENCES club_members(id),
    is_primary  INTEGER NOT NULL DEFAULT 0,  -- canonical handle/address per (member, surface)
    linked_by   TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (surface, identifier)
);
-- member_id indexes for these tables are created post-migration (see _ensure_member_indexes),
-- because on a pre-migration DB the member_id column doesn't exist yet.

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
    meeting_id  INTEGER PRIMARY KEY REFERENCES club_meetings(id),
    channel_id  TEXT,
    message_id  TEXT,
    status      TEXT NOT NULL DEFAULT 'open', -- open | closed
    opened_by   TEXT,
    opened_at   TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at   TEXT
);

CREATE TABLE IF NOT EXISTS meeting_attendance (
    meeting_id         INTEGER NOT NULL REFERENCES club_meetings(id),
    member_id          INTEGER NOT NULL REFERENCES club_members(id),
    status             TEXT NOT NULL,          -- yes | no | unsure
    source             TEXT NOT NULL DEFAULT 'button',
    updated_by_user_id TEXT,
    responded_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (meeting_id, member_id)
);
CREATE INDEX IF NOT EXISTS idx_attendance_meeting ON meeting_attendance(meeting_id);
CREATE INDEX IF NOT EXISTS idx_attendance_member ON meeting_attendance(member_id);

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
    meeting_id   INTEGER NOT NULL REFERENCES club_meetings(id),
    member_id    INTEGER NOT NULL REFERENCES club_members(id),
    status       TEXT NOT NULL,
    progress     TEXT,
    page         INTEGER,
    percent      INTEGER,
    source       TEXT,
    updated_by   TEXT,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (meeting_id, member_id)
);
CREATE INDEX IF NOT EXISTS idx_reading_statuses_meeting ON reading_statuses(meeting_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_reading_statuses_member ON reading_statuses(member_id);

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
    meeting_id  INTEGER NOT NULL REFERENCES club_meetings(id),
    member_id   INTEGER NOT NULL REFERENCES club_members(id),
    kind        TEXT NOT NULL, -- roll_call | reading_checkin | email_reply
    surface     TEXT NOT NULL, -- discord | email
    direction   TEXT NOT NULL, -- inbound | outbound
    status      TEXT NOT NULL, -- sent | received | skipped | failed
    subject     TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_member_contacts_meeting ON member_contacts(meeting_id, member_id, created_at);
CREATE INDEX IF NOT EXISTS idx_member_contacts_member ON member_contacts(member_id);

-- The mail archive attributes each message to a member via mail_messages.member_id
-- (FK → club_members, resolved through member_identities). There is no separate participant
-- identity store — club_members + member_identities is the single source of truth.

CREATE TABLE IF NOT EXISTS mail_threads (
    thread_id          TEXT PRIMARY KEY,
    list_id            TEXT,
    subject_normalized TEXT,
    first_sent_at      TEXT,
    last_sent_at       TEXT,
    message_count      INTEGER NOT NULL DEFAULT 0,
    participants_json  TEXT,
    summary            TEXT,
    summary_model      TEXT,
    summary_updated_at TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_mail_threads_last_sent ON mail_threads(last_sent_at);

CREATE TABLE IF NOT EXISTS mail_messages (
    message_id                 TEXT PRIMARY KEY,
    thread_id                  TEXT NOT NULL,
    parent_message_id          TEXT,
    source                     TEXT NOT NULL,
    source_ref                 TEXT,
    list_id                    TEXT,
    from_email                 TEXT,
    from_name                  TEXT,
    member_id                  INTEGER REFERENCES club_members(id),
    to_json                    TEXT,
    cc_json                    TEXT,
    reply_to_json              TEXT,
    subject                    TEXT,
    sent_at                    TEXT,
    received_at                TEXT,
    body_text                  TEXT,
    body_clean                 TEXT,
    body_html                  TEXT,
    attachments_json           TEXT,
    headers_json               TEXT,
    imported_at                TEXT NOT NULL DEFAULT (datetime('now')),
    processed_inbound_email_id TEXT,
    FOREIGN KEY(thread_id) REFERENCES mail_threads(thread_id)
);
CREATE INDEX IF NOT EXISTS idx_mail_messages_thread_sent ON mail_messages(thread_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_mail_messages_member_sent ON mail_messages(member_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_mail_messages_from_email ON mail_messages(from_email);
CREATE INDEX IF NOT EXISTS idx_mail_messages_sent_at ON mail_messages(sent_at);
CREATE INDEX IF NOT EXISTS idx_mail_messages_inbound_email
    ON mail_messages(processed_inbound_email_id);

CREATE VIRTUAL TABLE IF NOT EXISTS mail_message_fts USING fts5(
    message_id UNINDEXED,
    subject,
    from_name,
    from_email,
    body_clean
);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


# meeting_key (a book slug) → its meeting id; member_slug → member id. Correlated
# subqueries over the old row alias `o`, used by the one-time ops FK rebuild below.
_MK = ("(SELECT MAX(mb.meeting_id) FROM club_books b "
       "JOIN club_meeting_books mb ON mb.book_id = b.id WHERE b.slug = o.meeting_key)")
_MID = "(SELECT id FROM club_members WHERE slug = o.member_slug)"


def migrate_ops_to_fk(conn: sqlite3.Connection) -> None:
    """One-time: rebuild the ops tables from loose text keys (meeting_key=book slug,
    member_slug) onto integer FKs (meeting_id→club_meetings, member_id→club_members).
    Guarded (only runs on the old schema, once club_* is imported) and idempotent."""
    if "meeting_key" not in _columns(conn, "meeting_attendance"):
        return  # already migrated / fresh DB created on the new schema
    if not (_table_exists(conn, "club_books") and _table_exists(conn, "club_members")):
        return  # club record not imported yet — nothing to map against

    def _require_mappable(table: str, *, member: bool) -> None:
        cond = f"{_MK} IS NULL" + (f" OR {_MID} IS NULL" if member else "")
        bad = conn.execute(f"SELECT COUNT(*) c FROM {table} o WHERE {cond}").fetchone()["c"]
        if bad:
            raise RuntimeError(f"ops FK migration: {bad} row(s) in {table} do not map to a club id")

    _require_mappable("roll_calls", member=False)
    _require_mappable("meeting_attendance", member=True)
    _require_mappable("reading_statuses", member=True)
    _require_mappable("member_contacts", member=True)
    # email_tracking ids are nullable — map what resolves, leave the rest NULL.

    conn.commit()                                  # close any implicit tx so the PRAGMA takes effect
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript(f"""
        CREATE TABLE roll_calls_new (
            meeting_id INTEGER PRIMARY KEY REFERENCES club_meetings(id),
            channel_id TEXT, message_id TEXT, status TEXT NOT NULL DEFAULT 'open',
            opened_by TEXT, opened_at TEXT NOT NULL DEFAULT (datetime('now')), closed_at TEXT);
        INSERT INTO roll_calls_new(meeting_id, channel_id, message_id, status, opened_by, opened_at, closed_at)
            SELECT {_MK}, channel_id, message_id, status, opened_by, opened_at, closed_at FROM roll_calls o;

        CREATE TABLE meeting_attendance_new (
            meeting_id INTEGER NOT NULL REFERENCES club_meetings(id),
            member_id INTEGER NOT NULL REFERENCES club_members(id),
            status TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'button',
            updated_by_user_id TEXT, responded_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (meeting_id, member_id));
        INSERT INTO meeting_attendance_new(meeting_id, member_id, status, source, updated_by_user_id, responded_at)
            SELECT {_MK}, {_MID}, status, source, updated_by_user_id, responded_at FROM meeting_attendance o;

        CREATE TABLE reading_statuses_new (
            meeting_id INTEGER NOT NULL REFERENCES club_meetings(id),
            member_id INTEGER NOT NULL REFERENCES club_members(id),
            status TEXT NOT NULL, progress TEXT, page INTEGER, percent INTEGER,
            source TEXT, updated_by TEXT, updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (meeting_id, member_id));
        INSERT INTO reading_statuses_new(meeting_id, member_id, status, progress, page, percent, source, updated_by, updated_at)
            SELECT {_MK}, {_MID}, status, progress, page, percent, source, updated_by, updated_at FROM reading_statuses o;

        CREATE TABLE member_contacts_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL REFERENCES club_meetings(id),
            member_id INTEGER NOT NULL REFERENCES club_members(id),
            kind TEXT NOT NULL, surface TEXT NOT NULL, direction TEXT NOT NULL,
            status TEXT NOT NULL, subject TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')));
        INSERT INTO member_contacts_new(id, meeting_id, member_id, kind, surface, direction, status, subject, created_at)
            SELECT id, {_MK}, {_MID}, kind, surface, direction, status, subject, created_at FROM member_contacts o;

        CREATE TABLE email_tracking_new (
            token TEXT PRIMARY KEY, contact_id INTEGER,
            meeting_id INTEGER REFERENCES club_meetings(id),
            member_id INTEGER REFERENCES club_members(id),
            kind TEXT, subject TEXT, email_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY(contact_id) REFERENCES member_contacts(id));
        INSERT INTO email_tracking_new(token, contact_id, meeting_id, member_id, kind, subject, email_id, created_at)
            SELECT token, contact_id, {_MK}, {_MID}, kind, subject, email_id, created_at FROM email_tracking o;

        DROP TABLE roll_calls;          ALTER TABLE roll_calls_new          RENAME TO roll_calls;
        DROP TABLE meeting_attendance;  ALTER TABLE meeting_attendance_new  RENAME TO meeting_attendance;
        DROP TABLE reading_statuses;    ALTER TABLE reading_statuses_new    RENAME TO reading_statuses;
        DROP TABLE member_contacts;     ALTER TABLE member_contacts_new     RENAME TO member_contacts;
        DROP TABLE email_tracking;      ALTER TABLE email_tracking_new      RENAME TO email_tracking;
        CREATE INDEX IF NOT EXISTS idx_roll_calls_status ON roll_calls(status, opened_at);
        CREATE INDEX IF NOT EXISTS idx_attendance_meeting ON meeting_attendance(meeting_id);
        CREATE INDEX IF NOT EXISTS idx_reading_statuses_meeting ON reading_statuses(meeting_id, updated_at);
        CREATE INDEX IF NOT EXISTS idx_member_contacts_meeting ON member_contacts(meeting_id, member_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_email_tracking_meeting ON email_tracking(meeting_id, member_id);
        """)
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    dangling = conn.execute("PRAGMA foreign_key_check").fetchall()
    if dangling:
        raise RuntimeError(f"ops FK migration left dangling references: {[tuple(r) for r in dangling]}")


def migrate_identity_to_fk(conn: sqlite3.Connection) -> None:
    """One-time: move the identity/mail subsystem onto integer member_id FKs.

    - Fold member_identities (Discord) + member_emails into ONE member_identities table
      keyed by (surface, identifier) → member_id (the single email store; is_primary marks
      the canonical address that was club_members.email).
    - mail_participants / mail_messages / identity_claims: member_slug → member_id
      (nullable — lists/unknown senders have none). mail_participant_addresses drops its
      redundant member_slug (resolve via participant.member_id).
    - Drop the now-dead club_members.email / .mobile (the address lives in member_identities).
    Guarded on the old member_identities schema; idempotent.
    """
    if "discord_user_id" not in _columns(conn, "member_identities"):
        return  # already unified
    if not (_table_exists(conn, "club_members") and _table_exists(conn, "member_emails")):
        return
    sid = "(SELECT id FROM club_members WHERE slug = o.member_slug)"
    for tbl in ("member_identities", "member_emails"):
        bad = conn.execute(
            f"SELECT COUNT(*) c FROM {tbl} o WHERE {sid} IS NULL"
        ).fetchone()["c"]
        if bad:
            raise RuntimeError(f"identity migration: {bad} {tbl} rows do not map to a club member")

    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript(f"""
        CREATE TABLE member_identities_new (
            surface TEXT NOT NULL, identifier TEXT NOT NULL,
            member_id INTEGER NOT NULL REFERENCES club_members(id),
            is_primary INTEGER NOT NULL DEFAULT 0, linked_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (surface, identifier));
        INSERT INTO member_identities_new(surface, identifier, member_id, is_primary, linked_by, created_at, updated_at)
            SELECT 'discord', o.discord_user_id, {sid}, 0, o.linked_by, o.created_at, o.updated_at
            FROM member_identities o;
        INSERT INTO member_identities_new(surface, identifier, member_id, is_primary, linked_by, created_at, updated_at)
            SELECT 'email', o.email, {sid},
                   CASE WHEN (SELECT email FROM club_members WHERE slug = o.member_slug) = o.email THEN 1 ELSE 0 END,
                   o.linked_by, o.created_at, o.updated_at
            FROM member_emails o;
        -- Don't lose any club_members.email that wasn't already in member_emails (e.g. dan's,
        -- a former member). Add as a primary; OR IGNORE skips ones already folded above.
        INSERT OR IGNORE INTO member_identities_new(surface, identifier, member_id, is_primary, linked_by, created_at, updated_at)
            SELECT 'email', m.email, m.id, 1, 'club_members.email', datetime('now'), datetime('now')
            FROM club_members m WHERE m.email IS NOT NULL;
        DROP TABLE member_identities;
        DROP TABLE member_emails;
        ALTER TABLE member_identities_new RENAME TO member_identities;
        CREATE INDEX IF NOT EXISTS idx_member_identities_member ON member_identities(member_id, surface);

        DROP INDEX IF EXISTS idx_mail_participants_slug;
        ALTER TABLE mail_participants ADD COLUMN member_id INTEGER REFERENCES club_members(id);
        UPDATE mail_participants SET member_id = (SELECT id FROM club_members WHERE slug = member_slug);
        ALTER TABLE mail_participants DROP COLUMN member_slug;
        ALTER TABLE mail_participants DROP COLUMN membership_status;
        CREATE INDEX IF NOT EXISTS idx_mail_participants_member ON mail_participants(member_id);

        DROP INDEX IF EXISTS idx_mail_participant_addresses_slug;
        ALTER TABLE mail_participant_addresses DROP COLUMN member_slug;

        DROP INDEX IF EXISTS idx_mail_messages_member_sent;
        ALTER TABLE mail_messages ADD COLUMN member_id INTEGER REFERENCES club_members(id);
        UPDATE mail_messages SET member_id = (SELECT id FROM club_members WHERE slug = member_slug);
        ALTER TABLE mail_messages DROP COLUMN member_slug;
        CREATE INDEX IF NOT EXISTS idx_mail_messages_member_sent ON mail_messages(member_id, sent_at);

        ALTER TABLE identity_claims ADD COLUMN candidate_member_id INTEGER REFERENCES club_members(id);
        UPDATE identity_claims SET candidate_member_id = (SELECT id FROM club_members WHERE slug = candidate_member_slug);
        ALTER TABLE identity_claims DROP COLUMN candidate_member_slug;

        ALTER TABLE club_members DROP COLUMN email;
        ALTER TABLE club_members DROP COLUMN mobile;
        """)
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    dangling = conn.execute("PRAGMA foreign_key_check").fetchall()
    if dangling:
        raise RuntimeError(f"identity migration left dangling references: {[tuple(r) for r in dangling]}")


def migrate_drop_legacy_identity(conn: sqlite3.Connection) -> None:
    """One-time: remove the redundant identity surfaces, converging on the single model
    (club_members + member_identities).

    - Drop the mail archive's parallel person store (mail_participants,
      mail_participant_addresses) and mail_messages.sender_participant_id. Member attribution
      already lives in mail_messages.member_id (FK → club_members), which is the only column
      the read paths use; participants were write-only structural state.
    - Drop identity_claims (a write-only staging table with no acceptance flow).
    Guarded on mail_messages still having sender_participant_id; idempotent.
    """
    if "sender_participant_id" not in _columns(conn, "mail_messages"):
        return  # already collapsed / fresh DB on the new schema

    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript("""
        CREATE TABLE mail_messages_new (
            message_id                 TEXT PRIMARY KEY,
            thread_id                  TEXT NOT NULL,
            parent_message_id          TEXT,
            source                     TEXT NOT NULL,
            source_ref                 TEXT,
            list_id                    TEXT,
            from_email                 TEXT,
            from_name                  TEXT,
            member_id                  INTEGER REFERENCES club_members(id),
            to_json                    TEXT,
            cc_json                    TEXT,
            reply_to_json              TEXT,
            subject                    TEXT,
            sent_at                    TEXT,
            received_at                TEXT,
            body_text                  TEXT,
            body_clean                 TEXT,
            body_html                  TEXT,
            attachments_json           TEXT,
            headers_json               TEXT,
            imported_at                TEXT NOT NULL DEFAULT (datetime('now')),
            processed_inbound_email_id TEXT,
            FOREIGN KEY(thread_id) REFERENCES mail_threads(thread_id));
        INSERT INTO mail_messages_new
            (message_id, thread_id, parent_message_id, source, source_ref, list_id, from_email,
             from_name, member_id, to_json, cc_json, reply_to_json, subject, sent_at, received_at,
             body_text, body_clean, body_html, attachments_json, headers_json, imported_at,
             processed_inbound_email_id)
            SELECT message_id, thread_id, parent_message_id, source, source_ref, list_id, from_email,
             from_name, member_id, to_json, cc_json, reply_to_json, subject, sent_at, received_at,
             body_text, body_clean, body_html, attachments_json, headers_json, imported_at,
             processed_inbound_email_id
            FROM mail_messages;
        DROP TABLE mail_messages;
        ALTER TABLE mail_messages_new RENAME TO mail_messages;
        CREATE INDEX IF NOT EXISTS idx_mail_messages_thread_sent ON mail_messages(thread_id, sent_at);
        CREATE INDEX IF NOT EXISTS idx_mail_messages_member_sent ON mail_messages(member_id, sent_at);
        CREATE INDEX IF NOT EXISTS idx_mail_messages_from_email ON mail_messages(from_email);
        CREATE INDEX IF NOT EXISTS idx_mail_messages_sent_at ON mail_messages(sent_at);
        CREATE INDEX IF NOT EXISTS idx_mail_messages_inbound_email
            ON mail_messages(processed_inbound_email_id);

        DROP TABLE IF EXISTS mail_participant_addresses;
        DROP TABLE IF EXISTS mail_participants;
        DROP TABLE IF EXISTS identity_claims;
        """)
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    dangling = conn.execute("PRAGMA foreign_key_check").fetchall()
    if dangling:
        raise RuntimeError(f"legacy-identity drop left dangling references: {[tuple(r) for r in dangling]}")


def migrate_drop_email_tracking(conn: sqlite3.Connection) -> None:
    """One-time: remove the email open-tracking system for member privacy.

    Drops `email_opens` (the open log) and `email_tracking` (the per-email token table) — Oliver
    no longer records whether members open emails (no pixel, no external poll). The operational
    `member_contacts` outreach log is unaffected (it was the only inbound FK target and stays).
    Guarded on `email_tracking` still existing; idempotent.
    """
    if not _table_exists(conn, "email_tracking"):
        return

    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript("""
        DROP TABLE IF EXISTS email_opens;
        DROP TABLE IF EXISTS email_tracking;
        """)
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    dangling = conn.execute("PRAGMA foreign_key_check").fetchall()
    if dangling:
        raise RuntimeError(f"email-tracking drop left dangling references: {[tuple(r) for r in dangling]}")


def _ensure_member_indexes(conn: sqlite3.Connection) -> None:
    """Indexes on member_id columns that only exist once the table is in its new shape
    (fresh DB via _SCHEMA, or existing DB via migrate_identity_to_fk). Safe + idempotent."""
    if "member_id" in _columns(conn, "member_identities"):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_member_identities_member ON member_identities(member_id, surface)")


def _ensure_schema() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)
        migrate_ops_to_fk(conn)
        migrate_identity_to_fk(conn)
        migrate_drop_legacy_identity(conn)
        migrate_drop_email_tracking(conn)
        _ensure_member_indexes(conn)


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


# ── Member identity map (one table: Discord ids + emails → member_id) ─────────
# Storage is keyed by member_id (FK to club_members); the helpers keep a slug-based
# interface because callers build a 'member:<slug>' speaker string / look up the corpus
# member by slug. Slug↔id is resolved at this boundary, ids are the stored link.
def _member_id_for_slug(conn: sqlite3.Connection, slug: str | None) -> int | None:
    if not slug:
        return None
    r = conn.execute("SELECT id FROM club_members WHERE slug = ?", (slug,)).fetchone()
    return r["id"] if r else None


def link_identity(surface: str, identifier: str, member_slug: str, *,
                  is_primary: bool = False, linked_by: str | None = None) -> None:
    with connect() as conn:
        mid = _member_id_for_slug(conn, member_slug)
        if mid is None:
            raise ValueError(f"no club member with slug {member_slug!r}")
        conn.execute(
            "INSERT INTO member_identities (surface, identifier, member_id, is_primary, linked_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(surface, identifier) DO UPDATE SET "
            "member_id=excluded.member_id, is_primary=excluded.is_primary, "
            "linked_by=excluded.linked_by, updated_at=excluded.updated_at",
            (surface, identifier, mid, 1 if is_primary else 0, linked_by, _now()),
        )


def member_id_for_identity(surface: str, identifier: str | None) -> int | None:
    if not identifier:
        return None
    with connect() as conn:
        r = conn.execute(
            "SELECT member_id FROM member_identities WHERE surface = ? AND identifier = ?",
            (surface, identifier),
        ).fetchone()
    return r["member_id"] if r else None


def link_member_identity(discord_user_id: str, member_slug: str, *, linked_by: str | None = None) -> None:
    link_identity("discord", discord_user_id, member_slug, linked_by=linked_by)


def member_slug_for_user(discord_user_id: str | None) -> str | None:
    if not discord_user_id:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT m.slug FROM member_identities mi JOIN club_members m ON m.id = mi.member_id "
            "WHERE mi.surface = 'discord' AND mi.identifier = ?",
            (discord_user_id,),
        ).fetchone()
    return row["slug"] if row else None


def list_member_identities() -> list[dict]:
    """Discord links with member_slug projected (callers + admin display)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT mi.identifier AS discord_user_id, m.slug AS member_slug, "
            "mi.linked_by, mi.created_at, mi.updated_at "
            "FROM member_identities mi JOIN club_members m ON m.id = mi.member_id "
            "WHERE mi.surface = 'discord' ORDER BY m.slug"
        ).fetchall()
    return [dict(r) for r in rows]


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def link_member_email(email: str, member_slug: str, *, linked_by: str | None = None,
                      is_primary: bool = False) -> None:
    email = _normalize_email(email)
    if not email or "@" not in email:
        raise ValueError("email must look like an email address")
    link_identity("email", email, member_slug, is_primary=is_primary, linked_by=linked_by)


def member_slug_for_email(email: str | None) -> str | None:
    if not email:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT m.slug FROM member_identities mi JOIN club_members m ON m.id = mi.member_id "
            "WHERE mi.surface = 'email' AND mi.identifier = ?",
            (_normalize_email(email),),
        ).fetchone()
    return row["slug"] if row else None


def email_for_member(member_slug: str) -> dict | None:
    """The member's primary email as {email, member_slug}, or None. Primary first."""
    with connect() as conn:
        row = conn.execute(
            "SELECT mi.identifier AS email, m.slug AS member_slug, mi.is_primary, mi.linked_by "
            "FROM member_identities mi JOIN club_members m ON m.id = mi.member_id "
            "WHERE mi.surface = 'email' AND m.slug = ? "
            "ORDER BY mi.is_primary DESC, mi.updated_at DESC LIMIT 1",
            (member_slug,),
        ).fetchone()
    return dict(row) if row else None


def emails_for_member(member_slug: str) -> list[str]:
    """All of a member's email addresses, primary first."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT mi.identifier AS email FROM member_identities mi "
            "JOIN club_members m ON m.id = mi.member_id "
            "WHERE mi.surface = 'email' AND m.slug = ? ORDER BY mi.is_primary DESC, mi.identifier",
            (member_slug,),
        ).fetchall()
    return [r["email"] for r in rows]


def list_member_emails() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT mi.identifier AS email, m.slug AS member_slug, mi.is_primary, "
            "mi.linked_by, mi.created_at, mi.updated_at "
            "FROM member_identities mi JOIN club_members m ON m.id = mi.member_id "
            "WHERE mi.surface = 'email' ORDER BY m.slug, mi.identifier"
        ).fetchall()
    return [dict(r) for r in rows]


def _normalize_phone(number: str) -> str:
    """Keep a leading '+' and digits only — a loose E.164-ish normal form for dedup."""
    number = number.strip()
    digits = re.sub(r"[^\d]", "", number)
    return ("+" + digits) if number.startswith("+") else digits


def link_member_sms(number: str, member_slug: str, *, linked_by: str | None = None,
                    is_primary: bool = False) -> None:
    normalized = _normalize_phone(number)
    if len(re.sub(r"\D", "", normalized)) < 7:
        raise ValueError("phone number must have at least 7 digits")
    link_identity("sms", normalized, member_slug, is_primary=is_primary, linked_by=linked_by)


def sms_for_member(member_slug: str) -> list[str]:
    """All of a member's phone numbers, primary first."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT mi.identifier AS number FROM member_identities mi "
            "JOIN club_members m ON m.id = mi.member_id "
            "WHERE mi.surface = 'sms' AND m.slug = ? ORDER BY mi.is_primary DESC, mi.identifier",
            (member_slug,),
        ).fetchall()
    return [r["number"] for r in rows]


def list_member_sms() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT mi.identifier AS number, m.slug AS member_slug, mi.is_primary, "
            "mi.linked_by, mi.created_at, mi.updated_at "
            "FROM member_identities mi JOIN club_members m ON m.id = mi.member_id "
            "WHERE mi.surface = 'sms' ORDER BY m.slug, mi.identifier"
        ).fetchall()
    return [dict(r) for r in rows]


# ── Mail archive ─────────────────────────────────────────────────────────────
def _json_or_none(value) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def upsert_mail_message(message: dict) -> bool:
    """Insert or update one normalized archive message.

    Returns True when the message_id was new, False when it replaced an
    existing archive row.
    """
    message_id = str(message["message_id"])
    thread_id = str(message["thread_id"])
    from_email = _normalize_email(message.get("from_email") or "") or None
    member_slug = message.get("member_slug")
    with connect() as conn:
        member_id = _member_id_for_slug(conn, member_slug)
        existed = conn.execute(
            "SELECT 1 FROM mail_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone() is not None
        conn.execute(
            "INSERT INTO mail_threads "
            "(thread_id, list_id, subject_normalized, first_sent_at, last_sent_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(thread_id) DO UPDATE SET "
            "list_id=COALESCE(excluded.list_id, mail_threads.list_id), "
            "subject_normalized=COALESCE(mail_threads.subject_normalized, excluded.subject_normalized), "
            "updated_at=excluded.updated_at",
            (
                thread_id, message.get("list_id"), message.get("subject_normalized"),
                message.get("sent_at"), message.get("sent_at"), _now(),
            ),
        )
        cols = [
            "message_id", "thread_id", "parent_message_id", "source", "source_ref",
            "list_id", "from_email", "from_name",
            "member_id", "to_json", "cc_json", "reply_to_json", "subject",
            "sent_at", "received_at", "body_text", "body_clean", "body_html",
            "attachments_json", "headers_json", "processed_inbound_email_id",
        ]
        row = {
            "message_id": message_id,
            "thread_id": thread_id,
            "parent_message_id": message.get("parent_message_id"),
            "source": message.get("source") or "historical_import",
            "source_ref": message.get("source_ref"),
            "list_id": message.get("list_id"),
            "from_email": from_email,
            "from_name": message.get("from_name"),
            "member_id": member_id,
            "to_json": _json_or_none(message.get("to")),
            "cc_json": _json_or_none(message.get("cc")),
            "reply_to_json": _json_or_none(message.get("reply_to")),
            "subject": message.get("subject"),
            "sent_at": message.get("sent_at"),
            "received_at": message.get("received_at"),
            "body_text": message.get("body_text"),
            "body_clean": message.get("body_clean"),
            "body_html": message.get("body_html"),
            "attachments_json": _json_or_none(message.get("attachments")),
            "headers_json": _json_or_none(message.get("headers")),
            "processed_inbound_email_id": message.get("processed_inbound_email_id"),
        }
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(
            f"{c}=excluded.{c}" for c in cols
            if c not in {"message_id", "imported_at"}
        )
        conn.execute(
            f"INSERT INTO mail_messages ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(message_id) DO UPDATE SET {updates}",
            [row[c] for c in cols],
        )
        conn.execute("DELETE FROM mail_message_fts WHERE message_id = ?", (message_id,))
        conn.execute(
            "INSERT INTO mail_message_fts "
            "(message_id, subject, from_name, from_email, body_clean) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                message_id, row["subject"] or "", row["from_name"] or "",
                row["from_email"] or "", row["body_clean"] or "",
            ),
        )
        return not existed


def rebuild_mail_thread_stats() -> None:
    with connect() as conn:
        thread_ids = [
            r["thread_id"] for r in conn.execute(
                "SELECT DISTINCT thread_id FROM mail_messages ORDER BY thread_id"
            )
        ]
        for thread_id in thread_ids:
            stats = conn.execute(
                "SELECT COUNT(*) AS c, MIN(sent_at) AS first_sent_at, MAX(sent_at) AS last_sent_at "
                "FROM mail_messages WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            participants = [
                dict(r) for r in conn.execute(
                    "SELECT cm.slug AS member_slug, mm.from_email, mm.from_name, "
                    "COUNT(*) AS message_count "
                    "FROM mail_messages mm LEFT JOIN club_members cm ON cm.id = mm.member_id "
                    "WHERE mm.thread_id = ? "
                    "GROUP BY cm.slug, mm.from_email, mm.from_name "
                    "ORDER BY message_count DESC, mm.from_email",
                    (thread_id,),
                )
            ]
            conn.execute(
                "UPDATE mail_threads SET first_sent_at = ?, last_sent_at = ?, "
                "message_count = ?, participants_json = ?, updated_at = ? "
                "WHERE thread_id = ?",
                (
                    stats["first_sent_at"], stats["last_sent_at"], stats["c"],
                    _json_or_none(participants), _now(), thread_id,
                ),
            )


def mail_archive_counts() -> dict:
    with connect() as conn:
        return {
            "messages": conn.execute("SELECT COUNT(*) c FROM mail_messages").fetchone()["c"],
            "threads": conn.execute("SELECT COUNT(*) c FROM mail_threads").fetchone()["c"],
            "attributed": conn.execute(
                "SELECT COUNT(*) c FROM mail_messages WHERE member_id IS NOT NULL").fetchone()["c"],
        }


def mail_senders_for_reattribution(email: str | None = None) -> list[dict]:
    """Archived messages' (message_id, from_email, from_name, member_id) for re-resolving the
    sender → member link. Scope to one normalized address when given (cheap, indexed)."""
    sql = ("SELECT message_id, from_email, from_name, member_id FROM mail_messages "
           "WHERE from_email IS NOT NULL")
    args: list = []
    if email:
        sql += " AND from_email = ?"
        args.append(_normalize_email(email))
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def set_mail_message_member(message_id: str, member_slug: str | None) -> bool:
    """Point a message at a member (by slug → id), or NULL it. Returns True if it changed."""
    with connect() as conn:
        member_id = _member_id_for_slug(conn, member_slug)
        cur = conn.execute(
            "UPDATE mail_messages SET member_id = ? WHERE message_id = ? "
            "AND member_id IS NOT ?",
            (member_id, message_id, member_id),
        )
        return cur.rowcount > 0


def _fts_query(query: str) -> str:
    terms = [t for t in re.findall(r"[\w@.+-]+", query or "") if t]
    return " AND ".join(f'"{t.replace(chr(34), chr(34) * 2)}"' for t in terms)


def search_mail_archive(query: str, *, member_slug: str | None = None,
                        year_from: int | None = None, year_to: int | None = None,
                        limit: int = 8) -> list[dict]:
    match = _fts_query(query)
    if not match:
        return []
    limit = max(1, min(int(limit), 20))
    sql = (
        "SELECT m.message_id, m.thread_id, m.subject, m.from_name, m.from_email, "
        "cm.slug AS member_slug, m.sent_at, m.received_at, "
        "snippet(mail_message_fts, 4, '[', ']', ' ... ', 18) AS snippet "
        "FROM mail_message_fts JOIN mail_messages m "
        "ON m.message_id = mail_message_fts.message_id "
        "LEFT JOIN club_members cm ON cm.id = m.member_id "
        "WHERE mail_message_fts MATCH ?"
    )
    args: list = [match]
    if member_slug:
        sql += " AND m.member_id = (SELECT id FROM club_members WHERE slug = ?)"; args.append(member_slug)
    if year_from:
        sql += " AND m.sent_at >= ?"; args.append(f"{int(year_from):04d}-01-01")
    if year_to:
        sql += " AND m.sent_at < ?"; args.append(f"{int(year_to) + 1:04d}-01-01")
    sql += " ORDER BY COALESCE(m.sent_at, m.received_at, m.imported_at) DESC LIMIT ?"
    args.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def get_mail_thread(thread_id: str, *, limit: int = 50) -> dict | None:
    limit = max(1, min(int(limit), 100))
    with connect() as conn:
        thread = conn.execute(
            "SELECT * FROM mail_threads WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        if not thread:
            return None
        rows = conn.execute(
            "SELECT m.message_id, m.from_name, m.from_email, cm.slug AS member_slug, "
            "m.subject, m.sent_at, m.body_clean FROM mail_messages m "
            "LEFT JOIN club_members cm ON cm.id = m.member_id "
            "WHERE m.thread_id = ? ORDER BY m.sent_at ASC, m.message_id ASC LIMIT ?",
            (thread_id, limit),
        ).fetchall()
    return {"thread": dict(thread), "messages": [dict(r) for r in rows]}


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
def upsert_roll_call(*, meeting_id: int, channel_id: str | None = None,
                     message_id: str | None = None, opened_by: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO roll_calls (meeting_id, channel_id, message_id, opened_by, status) "
            "VALUES (?, ?, ?, ?, 'open') "
            "ON CONFLICT(meeting_id) DO UPDATE SET "
            "channel_id=COALESCE(excluded.channel_id, roll_calls.channel_id), "
            "message_id=COALESCE(excluded.message_id, roll_calls.message_id), "
            "opened_by=COALESCE(excluded.opened_by, roll_calls.opened_by), "
            "status='open', closed_at=NULL",
            (meeting_id, channel_id, message_id, opened_by),
        )


def get_roll_call(meeting_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM roll_calls WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchone()
    return dict(row) if row else None


def close_roll_call(meeting_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE roll_calls SET status = 'closed', closed_at = ? "
            "WHERE meeting_id = ? AND status != 'closed'",
            (_now(), meeting_id),
        )
        return cur.rowcount > 0


def set_attendance(*, meeting_id: int, member_id: int, status: str,
                   updated_by_user_id: str | None = None, source: str = "button") -> None:
    if status not in {"yes", "no", "unsure"}:
        raise ValueError("attendance status must be yes, no, or unsure")
    with connect() as conn:
        conn.execute(
            "INSERT INTO meeting_attendance "
            "(meeting_id, member_id, status, source, updated_by_user_id, responded_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(meeting_id, member_id) DO UPDATE SET "
            "status=excluded.status, source=excluded.source, "
            "updated_by_user_id=excluded.updated_by_user_id, responded_at=excluded.responded_at",
            (meeting_id, member_id, status, source, updated_by_user_id, _now()),
        )


def attendance_for_meeting(meeting_id: int) -> list[dict]:
    """Attendance rows for a meeting. Each row links by integer ids and also carries
    the member's slug + name, projected for display only."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT a.meeting_id, a.member_id, m.slug AS member_slug, m.name AS member_name, "
            "a.status, a.source, a.updated_by_user_id, a.responded_at "
            "FROM meeting_attendance a JOIN club_members m ON m.id = a.member_id "
            "WHERE a.meeting_id = ? ORDER BY m.name",
            (meeting_id,),
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


def set_reading_status(*, meeting_id: int, member_id: int, status: str,
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
            "(meeting_id, member_id, status, progress, page, percent, source, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(meeting_id, member_id) DO UPDATE SET "
            "status=excluded.status, progress=excluded.progress, page=excluded.page, "
            "percent=excluded.percent, source=excluded.source, updated_by=excluded.updated_by, "
            "updated_at=excluded.updated_at",
            (meeting_id, member_id, status, progress, page, percent, source, updated_by, _now()),
        )


def reading_status_for_meeting(meeting_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT r.meeting_id, r.member_id, m.slug AS member_slug, m.name AS member_name, "
            "r.status, r.progress, r.page, r.percent, r.source, r.updated_by, r.updated_at "
            "FROM reading_statuses r JOIN club_members m ON m.id = r.member_id "
            "WHERE r.meeting_id = ? ORDER BY m.name",
            (meeting_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def reading_status_for_member(meeting_id: int, member_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT r.*, m.slug AS member_slug, m.name AS member_name "
            "FROM reading_statuses r JOIN club_members m ON m.id = r.member_id "
            "WHERE r.meeting_id = ? AND r.member_id = ?",
            (meeting_id, member_id),
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
def add_member_contact(*, meeting_id: int, member_id: int, kind: str,
                       surface: str, direction: str, status: str,
                       subject: str | None = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO member_contacts "
            "(meeting_id, member_id, kind, surface, direction, status, subject) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (meeting_id, member_id, kind, surface, direction, status, subject),
        )
        return cur.lastrowid


def update_member_contact_status(contact_id: int, status: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE member_contacts SET status = ? WHERE id = ?",
            (status, contact_id),
        )


def member_contacts_for_meeting(meeting_id: int, *, limit: int = 200) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT c.*, m.slug AS member_slug, m.name AS member_name "
            "FROM member_contacts c JOIN club_members m ON m.id = c.member_id "
            "WHERE c.meeting_id = ? "
            "ORDER BY datetime(c.created_at) DESC, c.id DESC LIMIT ?",
            (meeting_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


