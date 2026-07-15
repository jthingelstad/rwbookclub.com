"""Oliver's private memory & state — SQLite (class B).

Holds what doesn't belong in the club record or the generated corpus: durable notes Oliver learns,
per-channel conversation history + rolling summaries, reminders, and usage logs.
Gitignored, local to wherever Oliver runs; backup is a deployment concern.

Tables are created idempotently on import; ordered legacy transforms are recorded in
``schema_migrations`` and run once. Each helper opens a short-lived connection so the
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
from urllib.parse import urlsplit

from agent import security
from agent.repositories import jobs as _jobs_repo
from agent.repositories import outbox as _outbox_repo

DB_PATH = Path(os.environ.get("OLIVER_DB_PATH") or Path(__file__).resolve().parent / "oliver.db")
security.set_private_umask()

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

-- The Book Cloud: books the club orbits (mentions, comparisons, nominations, jokes) — usually
-- NOT books it has read. No dedupe: same title mentioned twice for two reasons = two rows; the
-- REASON is the cultural payload ("mentioned in chat" is a failed reason). Mentioner is resolved
-- from ctx via the identity map, never taken from model input. Private SQLite only — never
-- rendered to the corpus or website. (Designed in AGENT-TEAM/work/2026-06-26-build-book-cloud.md.)
CREATE TABLE IF NOT EXISTS book_cloud (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    title             TEXT NOT NULL,         -- mentioned book title (free text; often not a corpus book)
    author            TEXT,                  -- when known
    book_slug         TEXT,                  -- set only on a confident match to a corpus book; else NULL
    mentioned_by      TEXT,                  -- member slug (identity map); NULL if speaker unlinked
    mentioned_by_name TEXT,                  -- display-name fallback, provenance only
    surface           TEXT NOT NULL,         -- 'discord' | 'email' | 'mailing_list'
    channel_id        TEXT,
    source_message_id TEXT,
    reason            TEXT NOT NULL,         -- WHY it came up — the cultural payload
    reason_kind       TEXT,                  -- advisory: nomination|recommendation|comparison|caution|context|inquiry|joke (plus internal pick_candidate)
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_book_cloud_created ON book_cloud(created_at);
CREATE INDEX IF NOT EXISTS idx_book_cloud_title   ON book_cloud(title);

-- One identity map for every way we recognize / reach a member: Discord ids, email
-- addresses, and phone numbers (the single handle store). Keyed by member_id FK — slug is
-- never stored here. club_members is the person; member_identities holds their handles.
CREATE TABLE IF NOT EXISTS member_identities (
    surface     TEXT NOT NULL,               -- 'discord' | 'email' | 'sms' | 'website'
    identifier  TEXT NOT NULL,               -- discord id | normalized email | E.164-ish phone | url
    member_id   INTEGER NOT NULL REFERENCES club_members(id),
    is_primary  INTEGER NOT NULL DEFAULT 0,  -- canonical handle/address per (member, surface)
    label       TEXT,                        -- display name for website rows (e.g. 'Blog'); NULL otherwise
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
    member_slug TEXT,                             -- resolved member, for cross-medium recall
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_conv_channel ON conversations(channel_id, id);
-- idx_conv_member is created in _migrate(), after the member_slug column is ensured on old DBs.

CREATE TABLE IF NOT EXISTS channel_summaries (
    channel_id TEXT PRIMARY KEY,
    summary    TEXT NOT NULL,
    last_id    INTEGER NOT NULL DEFAULT 0,        -- highest conversations.id folded in
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Generic per-job state (cursors/watermarks) for scheduled jobs, e.g. the weekly reflection pass.
CREATE TABLE IF NOT EXISTS job_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,                     -- JSON blob owned by the job
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Ordered, auditable application migrations. CREATE TABLE schemas remain idempotent;
-- destructive/data-moving legacy transforms are recorded here and never reprobed after success.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    applied_at TEXT NOT NULL
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

-- One-time links for the member web app. A Discord command mints a row (the Discord
-- identity IS the auth); the local web server (reached via Tailscale Funnel) resolves the
-- token to a member. created_at/expires_at are ISO-8601 UTC; used_at is reserved for the
-- production single-use exchange (the spike leaves it null and allows reuse until expiry).
CREATE TABLE IF NOT EXISTS webapp_tokens (
    token       TEXT PRIMARY KEY,
    member_id   INTEGER NOT NULL,
    is_admin    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT
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

CREATE TABLE IF NOT EXISTS review_drafts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id   INTEGER NOT NULL,
    book_slug   TEXT NOT NULL,
    thread_id   TEXT,
    state       TEXT NOT NULL DEFAULT 'awaiting_reply',
                -- awaiting_reply | awaiting_confirm | written | declined | parked
    draft_json  TEXT,
    rounds      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_review_drafts_thread ON review_drafts(thread_id);
CREATE INDEX IF NOT EXISTS idx_review_drafts_member ON review_drafts(member_id, state);

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

CREATE TABLE IF NOT EXISTS outbox_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL,                 -- email | discord
    payload_json    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
                    -- pending | claimed | delivering | retry | delivered | uncertain | dead
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 5,
    available_at    TEXT NOT NULL,
    lease_owner     TEXT,
    lease_expires_at TEXT,
    provider_ref_json TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    delivered_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_status_available
    ON outbox_messages(status, available_at, id);
CREATE INDEX IF NOT EXISTS idx_outbox_lease
    ON outbox_messages(status, lease_expires_at);

CREATE TABLE IF NOT EXISTS job_leases (
    job_name        TEXT PRIMARY KEY,
    lease_owner     TEXT,
    lease_expires_at TEXT,
    acquired_at     TEXT,
    updated_at      TEXT NOT NULL,
    expected_interval_seconds INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS job_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name        TEXT NOT NULL,
    lease_owner     TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    outcome         TEXT NOT NULL DEFAULT 'running',
    duration_ms     INTEGER,
    processed_count INTEGER NOT NULL DEFAULT 0,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_runs_job_started
    ON job_runs(job_name, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_job_runs_outcome
    ON job_runs(outcome, finished_at DESC);

-- The club's append-only event log / timeline. Both member_id and meeting_id are NULLABLE:
-- a member+meeting event is meeting ops; a member-only event is a life event; an untagged event
-- is a club happening. `occurred_at` = when it happened/will happen (timeline), `created_at` = when
-- recorded; ops events leave them equal. `source` = provenance (e.g. 'mail:<thread_id>' for mined).
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id   INTEGER REFERENCES club_members(id),   -- NULL = group/club-wide
    meeting_id  INTEGER REFERENCES club_meetings(id),  -- NULL = not meeting-scoped
    actor       TEXT NOT NULL,                          -- oliver | member | admin
    category    TEXT NOT NULL,                          -- meeting_ops | meeting | selection | ...
    kind        TEXT NOT NULL,
    detail      TEXT,                                   -- scalar value or JSON
    surface     TEXT,                                   -- discord | email | system
    source      TEXT,                                   -- provenance (mail:<thread_id>, NULL=live)
    occurred_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_events_member   ON events(member_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_meeting  ON events(meeting_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_meeting_member ON events(meeting_id, member_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_meeting_kind ON events(meeting_id, kind, created_at);
CREATE INDEX IF NOT EXISTS idx_events_category ON events(category, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_occurred ON events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_source   ON events(source);

-- Current per-member status for a meeting, projected from the meeting_ops events. Sparse:
-- a missing row means 'unknown' for that member.
CREATE TABLE IF NOT EXISTS meeting_member_status (
    meeting_id             INTEGER NOT NULL REFERENCES club_meetings(id),
    member_id              INTEGER NOT NULL REFERENCES club_members(id),
    attendance             TEXT NOT NULL DEFAULT 'unknown',  -- unknown | yes | no | unsure
    reading                TEXT NOT NULL DEFAULT 'unknown',  -- unknown + the 6 READING_STATUSES
    reading_progress       TEXT,
    reading_page           INTEGER,
    reading_percent        INTEGER,
    attendance_asks        INTEGER NOT NULL DEFAULT 0,
    reading_asks           INTEGER NOT NULL DEFAULT 0,
    attendance_answered_at TEXT,
    reading_answered_at    TEXT,
    last_asked_at          TEXT,
    reading_last_asked_at  TEXT,
    updated_at             TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (meeting_id, member_id)
);
CREATE INDEX IF NOT EXISTS idx_meeting_member_status_meeting ON meeting_member_status(meeting_id);

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
    # Website rows carry a display label (e.g. "Blog") shown on the public profile.
    if "label" not in _columns(conn, "member_identities"):
        conn.execute("ALTER TABLE member_identities ADD COLUMN label TEXT")

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

    activity_cols = _columns(conn, "activity_events")
    activity_additions = {
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "last_error": "TEXT",
        "next_attempt_at": "TEXT",
    }
    for col, spec in activity_additions.items():
        if col not in activity_cols:
            conn.execute(f"ALTER TABLE activity_events ADD COLUMN {col} {spec}")

    # Tag each conversation turn with the resolved member so Oliver can recall a person's history
    # across mediums (Discord channel id vs email:{thread} are different channel_ids). Nullable —
    # unrecognized speakers and old rows stay NULL (a best-effort backfill fills what it can).
    if "member_slug" not in _columns(conn, "conversations"):
        conn.execute("ALTER TABLE conversations ADD COLUMN member_slug TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_member ON conversations(member_slug, id)")


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


def migrate_website_to_identities(conn: sqlite3.Connection) -> None:
    """One-time: fold the single `club_members.website` column into `member_identities`
    (surface='website') so a member can have multiple website URLs, like emails/phones. Mirrors
    migrate_identity_to_fk (which folded email/mobile). Guarded on the column still existing.
    """
    if "website" not in _columns(conn, "club_members"):
        return

    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript("""
        INSERT OR IGNORE INTO member_identities
            (surface, identifier, member_id, is_primary, linked_by, created_at, updated_at)
            SELECT 'website', website, id, 1, 'club_members.website', datetime('now'), datetime('now')
            FROM club_members WHERE website IS NOT NULL AND TRIM(website) != '';
        ALTER TABLE club_members DROP COLUMN website;
        """)
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    dangling = conn.execute("PRAGMA foreign_key_check").fetchall()
    if dangling:
        raise RuntimeError(f"website migration left dangling references: {[tuple(r) for r in dangling]}")


def migrate_drop_review_airtable_id(conn: sqlite3.Connection) -> None:
    """One-time: drop the vestigial `club_reviews.airtable_id`. It was an Airtable record id
    repurposed as the corpus review `id`, but the integer PK (`club_reviews.id`) is an equally
    stable, edit-surviving identity and nothing public references the old value. Guarded on the
    column existing; the corpus review `id` now comes from `club_reviews.id`.
    """
    if "airtable_id" not in _columns(conn, "club_reviews"):
        return

    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript("ALTER TABLE club_reviews DROP COLUMN airtable_id;")
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    dangling = conn.execute("PRAGMA foreign_key_check").fetchall()
    if dangling:
        raise RuntimeError(f"review airtable_id drop left dangling references: {[tuple(r) for r in dangling]}")


def migrate_meeting_events(conn: sqlite3.Connection) -> None:
    """One-time: collapse the four meeting-ops tables (meeting_attendance, reading_statuses,
    member_contacts, roll_calls) into the unified `events` log + the `meeting_member_status`
    projection. Preserves current per-member status and best-effort backfills the event history
    (timestamps from the source rows), then drops the four tables. Guarded on member_contacts
    existing; idempotent. (`events`/`meeting_member_status` are created by _SCHEMA first.)
    """
    if not _table_exists(conn, "member_contacts"):
        return

    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript("""
        -- 1. Seed the projection: current attendance, then merge current reading.
        -- (WHERE true disambiguates ON CONFLICT after a SELECT, per the SQLite parser.)
        INSERT INTO meeting_member_status (meeting_id, member_id, attendance, attendance_answered_at, updated_at)
            SELECT meeting_id, member_id, status, responded_at, responded_at FROM meeting_attendance WHERE true
        ON CONFLICT(meeting_id, member_id) DO UPDATE SET
            attendance=excluded.attendance, attendance_answered_at=excluded.attendance_answered_at;
        INSERT INTO meeting_member_status (meeting_id, member_id, reading, reading_progress, reading_page, reading_percent, reading_answered_at, updated_at)
            SELECT meeting_id, member_id, status, progress, page, percent, updated_at, updated_at FROM reading_statuses WHERE true
        ON CONFLICT(meeting_id, member_id) DO UPDATE SET
            reading=excluded.reading, reading_progress=excluded.reading_progress,
            reading_page=excluded.reading_page, reading_percent=excluded.reading_percent,
            reading_answered_at=excluded.reading_answered_at;
        -- ensure a row for anyone Oliver contacted but who never answered (so asks counts survive)
        INSERT INTO meeting_member_status (meeting_id, member_id)
            SELECT DISTINCT meeting_id, member_id FROM member_contacts WHERE true
        ON CONFLICT(meeting_id, member_id) DO NOTHING;
        -- ask counts + last-asked from SENT outbound contacts (an "ask" = a delivered request,
        -- matching the old campaign count and how record_*_request bumps the counter going forward).
        UPDATE meeting_member_status SET
            attendance_asks = (SELECT COUNT(*) FROM member_contacts c WHERE c.meeting_id=meeting_member_status.meeting_id AND c.member_id=meeting_member_status.member_id AND c.kind='roll_call' AND c.direction='outbound' AND c.status='sent'),
            reading_asks = (SELECT COUNT(*) FROM member_contacts c WHERE c.meeting_id=meeting_member_status.meeting_id AND c.member_id=meeting_member_status.member_id AND c.kind='reading_checkin' AND c.direction='outbound' AND c.status='sent'),
            last_asked_at = (SELECT MAX(c.created_at) FROM member_contacts c WHERE c.meeting_id=meeting_member_status.meeting_id AND c.member_id=meeting_member_status.member_id AND c.direction='outbound' AND c.status='sent'),
            reading_last_asked_at = (SELECT MAX(c.created_at) FROM member_contacts c WHERE c.meeting_id=meeting_member_status.meeting_id AND c.member_id=meeting_member_status.member_id AND c.kind='reading_checkin' AND c.direction='outbound' AND c.status='sent');

        -- 2. Backfill the event history (occurred_at = created_at = source timestamp).
        INSERT INTO events (member_id, meeting_id, actor, category, kind, detail, surface, occurred_at, created_at)
            SELECT member_id, meeting_id, 'member', 'meeting_ops', 'attendance_reported', status, source, responded_at, responded_at FROM meeting_attendance;
        INSERT INTO events (member_id, meeting_id, actor, category, kind, detail, surface, occurred_at, created_at)
            SELECT member_id, meeting_id, 'member', 'meeting_ops', 'reading_reported',
                   json_object('status', status, 'progress', progress, 'page', page, 'percent', percent),
                   source, updated_at, updated_at FROM reading_statuses;
        INSERT INTO events (member_id, meeting_id, actor, category, kind, surface, occurred_at, created_at)
            SELECT member_id, meeting_id, 'oliver', 'meeting_ops',
                   CASE kind WHEN 'reading_checkin' THEN 'reading_requested' ELSE 'attendance_requested' END,
                   surface, created_at, created_at FROM member_contacts WHERE direction='outbound' AND status='sent' AND kind IN ('roll_call','reading_checkin');
        INSERT INTO events (member_id, meeting_id, actor, category, kind, detail, surface, occurred_at, created_at)
            SELECT member_id, meeting_id, 'member', 'meeting_ops', 'email_reply', subject, surface, created_at, created_at FROM member_contacts WHERE direction='inbound';
        INSERT INTO events (meeting_id, actor, category, kind, detail, occurred_at, created_at)
            SELECT meeting_id, 'oliver', 'meeting_ops', 'roll_call_opened',
                   json_object('channel_id', channel_id, 'message_id', message_id, 'opened_by', opened_by), opened_at, opened_at FROM roll_calls;
        INSERT INTO events (meeting_id, actor, category, kind, occurred_at, created_at)
            SELECT meeting_id, 'oliver', 'meeting_ops', 'roll_call_closed', closed_at, closed_at FROM roll_calls WHERE status='closed' AND closed_at IS NOT NULL;

        -- 3. Drop the four old tables.
        DROP TABLE IF EXISTS member_contacts;
        DROP TABLE IF EXISTS meeting_attendance;
        DROP TABLE IF EXISTS reading_statuses;
        DROP TABLE IF EXISTS roll_calls;
        """)
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    dangling = conn.execute("PRAGMA foreign_key_check").fetchall()
    if dangling:
        raise RuntimeError(f"meeting-events migration left dangling references: {[tuple(r) for r in dangling]}")


def _ensure_member_indexes(conn: sqlite3.Connection) -> None:
    """Indexes on member_id columns that only exist once the table is in its new shape
    (fresh DB via _SCHEMA, or existing DB via migrate_identity_to_fk). Safe + idempotent."""
    if "member_id" in _columns(conn, "member_identities"):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_member_identities_member ON member_identities(member_id, surface)")


_MIGRATIONS = (
    (1, "additive_runtime_columns", _migrate),
    (2, "meeting_ops_foreign_keys", migrate_ops_to_fk),
    (3, "unified_member_identities", migrate_identity_to_fk),
    (4, "drop_legacy_identity_tables", migrate_drop_legacy_identity),
    (5, "member_websites_to_identities", migrate_website_to_identities),
    (6, "drop_review_airtable_id", migrate_drop_review_airtable_id),
    (7, "drop_email_open_tracking", migrate_drop_email_tracking),
    (8, "unified_meeting_events", migrate_meeting_events),
)


def _migration_ready(conn: sqlite3.Connection, version: int) -> bool:
    """Whether a legacy transform has the source schema needed to run or baseline safely."""
    if version == 2 and "meeting_key" in _columns(conn, "meeting_attendance"):
        return _table_exists(conn, "club_books") and _table_exists(conn, "club_members")
    if version == 3 and "discord_user_id" in _columns(conn, "member_identities"):
        return _table_exists(conn, "club_members") and _table_exists(conn, "member_emails")
    if version == 4 and "sender_participant_id" in _columns(conn, "mail_messages"):
        return "member_id" in _columns(conn, "mail_messages")
    if version == 5:
        return _table_exists(conn, "club_members")
    if version == 6:
        return _table_exists(conn, "club_reviews")
    return True


def _run_migrations(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT version, name FROM schema_migrations ORDER BY version"
    ).fetchall()
    applied = {int(row["version"]): row["name"] for row in rows}
    known_versions = {version for version, _, _ in _MIGRATIONS}
    unknown = sorted(set(applied) - known_versions)
    if unknown:
        raise RuntimeError(f"database has migrations newer than this code: {unknown}")
    if applied:
        expected = list(range(1, max(applied) + 1))
        if sorted(applied) != expected:
            raise RuntimeError(
                f"database migration ledger has a gap: {sorted(applied)}, expected {expected}"
            )
    for version, name, migration in _MIGRATIONS:
        if version in applied:
            if applied[version] != name:
                raise RuntimeError(
                    f"migration {version} is recorded as {applied[version]!r}, expected {name!r}"
                )
            continue
        # Migrations are strictly ordered. A pre-club legacy DB pauses here until clubdb creates
        # or imports the authoritative club tables, then clubdb.ensure_schema resumes the ledger.
        if not _migration_ready(conn, version):
            break
        migration(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (version, name, datetime.now(timezone.utc).isoformat()),
        )


def run_migrations() -> None:
    """Resume pending ordered migrations after another schema owner (clubdb) is ready."""
    with connect() as conn:
        _run_migrations(conn)
        _ensure_member_indexes(conn)


def _ensure_schema() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(_SCHEMA)
        _run_migrations(conn)
        _ensure_member_indexes(conn)


_ensure_schema()
security.secure_database_files(DB_PATH)


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
                 query: str | None = None, source: str | None = None,
                 limit: int = 50) -> list[dict]:
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
    if source:
        sql += " AND source = ?"; args.append(source)
    sql += " ORDER BY id DESC LIMIT ?"; args.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def visible_memories(*, viewer_member_slug: str | None, is_admin: bool,
                     subject: str | None = None, query: str | None = None,
                     limit: int = 50) -> list[dict]:
    """Memories a conversational actor may see.

    Admin repair/audit calls retain the unrestricted reader above.  A linked member sees club
    lore plus their own member-scoped notes; general notes and another member's notes stay private
    to Oliver/admin.  Unlinked callers get no rows.  The dispatcher also rejects unauthorized
    targets, but this query is the row-level authority if a caller forgets that check.
    """
    if is_admin:
        if subject == "club":
            return get_memories(scope="club", query=query, limit=limit)
        return get_memories(subject=subject, query=query, limit=limit)
    if not viewer_member_slug:
        return []
    if subject and subject not in {viewer_member_slug, "club"}:
        return []

    sql = (
        "SELECT id, scope, subject, note, source, source_user_id, source_message_id, "
        "confidence, created_at FROM memories WHERE status = 'active'"
    )
    args: list = []
    if subject == "club":
        sql += " AND scope = 'club'"
    elif subject == viewer_member_slug:
        sql += " AND scope = 'member' AND subject = ?"
        args.append(viewer_member_slug)
    else:
        sql += " AND (scope = 'club' OR (scope = 'member' AND subject = ?))"
        args.append(viewer_member_slug)
    if query:
        sql += " AND note LIKE ?"
        args.append(f"%{query}%")
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 100)))
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def count_memories() -> int:
    """Active memory count (the admin status card's one-number view of the memory store)."""
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM memories WHERE status = 'active'").fetchone()[0]


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


# ── Book Cloud ────────────────────────────────────────────────────────────────
def add_book_cloud_entry(*, title: str, reason: str, surface: str,
                         author: str | None = None, book_slug: str | None = None,
                         mentioned_by: str | None = None, mentioned_by_name: str | None = None,
                         channel_id: str | None = None, source_message_id: str | None = None,
                         reason_kind: str | None = None, created_at: str | None = None) -> int:
    """Unconditional INSERT (no dedupe — the reason is the unit of value). `created_at` may be
    supplied so the archive seeding can BACKDATE mentions to their real sent date, keeping
    first/last-mention aggregation historically true."""
    if not (title or "").strip() or not (reason or "").strip():
        raise ValueError("a book_cloud entry needs both a title and a reason")
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO book_cloud (title, author, book_slug, mentioned_by, mentioned_by_name, "
            "surface, channel_id, source_message_id, reason, reason_kind, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))",
            (title.strip(), author, book_slug, mentioned_by, mentioned_by_name, surface,
             channel_id, source_message_id, reason.strip(), reason_kind, created_at),
        )
        return cur.lastrowid


def recent_book_cloud(*, limit: int = 20, query: str | None = None,
                      member: str | None = None, kind: str | None = None) -> list[dict]:
    """Raw cloud rows, newest first; `query` is a LIKE over title/author/reason; `member`/`kind`
    filter by mentioner slug and reason_kind (the admin webapp view uses all three)."""
    sql = ("SELECT id, title, author, book_slug, mentioned_by, mentioned_by_name, surface, "
           "reason, reason_kind, created_at FROM book_cloud")
    where: list[str] = []
    args: list = []
    if query:
        where.append("(title LIKE ? OR author LIKE ? OR reason LIKE ?)")
        args += [f"%{query}%"] * 3
    if member:
        where.append("mentioned_by = ?")
        args.append(member)
    if kind:
        where.append("reason_kind = ?")
        args.append(kind)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def book_cloud_titles(*, query: str | None = None, member: str | None = None,
                      limit: int = 50) -> list[dict]:
    """The AGGREGATED cloud — one row per (normalized) title: first/last mention, count, who,
    recent reasons. This is the 'books orbiting the club' view (raw rows stay un-deduped)."""
    sql = ("SELECT lower(trim(title)) AS k, MAX(title) AS title, MAX(author) AS author, "
           "MAX(book_slug) AS book_slug, MIN(created_at) AS first_mentioned, "
           "MAX(created_at) AS last_mentioned, COUNT(*) AS mention_count "
           "FROM book_cloud")
    args: list = []
    where = []
    if query:
        where.append("(title LIKE ? OR author LIKE ? OR reason LIKE ?)")
        args += [f"%{query}%"] * 3
    if member:
        where.append("k IN (SELECT lower(trim(title)) FROM book_cloud WHERE mentioned_by = ?)")
        args.append(member)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY k ORDER BY last_mentioned DESC LIMIT ?"
    args.append(max(1, min(int(limit), 200)))
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, args)]
        for r in rows:
            detail = conn.execute(
                "SELECT mentioned_by, reason FROM book_cloud WHERE lower(trim(title)) = ? "
                "ORDER BY id DESC LIMIT 6", (r.pop("k"),)).fetchall()
            r["mentioners"] = sorted({d["mentioned_by"] for d in detail if d["mentioned_by"]})
            r["recentReasons"] = [d["reason"] for d in detail[:3]]
    return rows


def recent_book_cloud_visible(*, viewer_member_slug: str | None, is_admin: bool,
                              limit: int = 20, query: str | None = None,
                              member: str | None = None, kind: str | None = None) -> list[dict]:
    """Book-cloud rows visible to a conversational actor.

    Discord/mailing-list mentions are club-shared.  A mention captured in 1:1 email
    (``surface='email'``) remains visible only to its member; admin views keep using the raw reader.
    """
    if is_admin:
        return recent_book_cloud(limit=limit, query=query, member=member, kind=kind)
    if not viewer_member_slug:
        return []
    sql = ("SELECT id, title, author, book_slug, mentioned_by, mentioned_by_name, surface, "
           "reason, reason_kind, created_at FROM book_cloud WHERE "
           "(COALESCE(surface, '') != 'email' OR mentioned_by = ?)")
    args: list = [viewer_member_slug]
    if query:
        sql += " AND (title LIKE ? OR author LIKE ? OR reason LIKE ?)"
        args += [f"%{query}%"] * 3
    if member:
        sql += " AND mentioned_by = ?"
        args.append(member)
    if kind:
        sql += " AND reason_kind = ?"
        args.append(kind)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def book_cloud_titles_visible(*, viewer_member_slug: str | None, is_admin: bool,
                              query: str | None = None, member: str | None = None,
                              limit: int = 50) -> list[dict]:
    """Aggregated actor-scoped Book Cloud; see ``recent_book_cloud_visible``."""
    if is_admin:
        return book_cloud_titles(query=query, member=member, limit=limit)
    if not viewer_member_slug:
        return []
    visible = "(COALESCE(surface, '') != 'email' OR mentioned_by = ?)"
    sql = (
        "SELECT lower(trim(title)) AS k, MAX(title) AS title, MAX(author) AS author, "
        "MAX(book_slug) AS book_slug, MIN(created_at) AS first_mentioned, "
        "MAX(created_at) AS last_mentioned, COUNT(*) AS mention_count FROM book_cloud "
        f"WHERE {visible}"
    )
    args: list = [viewer_member_slug]
    if query:
        sql += " AND (title LIKE ? OR author LIKE ? OR reason LIKE ?)"
        args += [f"%{query}%"] * 3
    if member:
        sql += (
            " AND lower(trim(title)) IN (SELECT lower(trim(title)) FROM book_cloud "
            f"WHERE mentioned_by = ? AND {visible})"
        )
        args += [member, viewer_member_slug]
    sql += " GROUP BY k ORDER BY last_mentioned DESC LIMIT ?"
    args.append(max(1, min(int(limit), 200)))
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, args)]
        for row in rows:
            key = row.pop("k")
            detail = conn.execute(
                "SELECT mentioned_by, reason FROM book_cloud WHERE lower(trim(title)) = ? "
                f"AND {visible} ORDER BY id DESC LIMIT 6",
                (key, viewer_member_slug),
            ).fetchall()
            row["mentioners"] = sorted({d["mentioned_by"] for d in detail if d["mentioned_by"]})
            row["recentReasons"] = [d["reason"] for d in detail[:3]]
    return rows


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
                  is_primary: bool = False, linked_by: str | None = None,
                  label: str | None = None) -> None:
    with connect() as conn:
        mid = _member_id_for_slug(conn, member_slug)
        if mid is None:
            raise ValueError(f"no club member with slug {member_slug!r}")
        conn.execute(
            "INSERT INTO member_identities (surface, identifier, member_id, is_primary, linked_by, label, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(surface, identifier) DO UPDATE SET "
            "member_id=excluded.member_id, is_primary=excluded.is_primary, linked_by=excluded.linked_by, "
            "label=COALESCE(excluded.label, member_identities.label), updated_at=excluded.updated_at",
            (surface, identifier, mid, 1 if is_primary else 0, linked_by, (label or "").strip() or None, _now()),
        )


def member_handles(member_slug: str, surface: str) -> list[dict]:
    """A member's handles for a surface with their primary flag, primary-first."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT mi.identifier, mi.is_primary, mi.label FROM member_identities mi "
            "JOIN club_members m ON m.id = mi.member_id "
            "WHERE m.slug = ? AND mi.surface = ? ORDER BY mi.is_primary DESC, mi.identifier",
            (member_slug, surface),
        ).fetchall()
    return [{"identifier": r["identifier"], "is_primary": bool(r["is_primary"]), "label": r["label"]}
            for r in rows]


def set_primary_identity(member_slug: str, surface: str, identifier: str) -> bool:
    """Mark one handle primary for (member, surface), clearing the flag on the others. Returns True
    if the identifier belongs to this member + surface."""
    with connect() as conn:
        mid = _member_id_for_slug(conn, member_slug)
        if mid is None:
            return False
        owned = conn.execute(
            "SELECT 1 FROM member_identities WHERE surface = ? AND identifier = ? AND member_id = ?",
            (surface, identifier, mid)).fetchone()
        if not owned:
            return False
        conn.execute("UPDATE member_identities SET is_primary = 0 WHERE surface = ? AND member_id = ?",
                     (surface, mid))
        conn.execute("UPDATE member_identities SET is_primary = 1, updated_at = ? "
                     "WHERE surface = ? AND identifier = ?", (_now(), surface, identifier))
    return True


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


def _normalize_url(url: str) -> str:
    """Light normal form for a website URL: trim, default to https:// when no scheme is given, drop a
    trailing slash. Shape validation (a host with a dot) happens in link_member_website."""
    url = url.strip()
    if url and "://" not in url:
        url = "https://" + url
    return url.rstrip("/")


def _require_web_url(url: str) -> str:
    """Normalize + validate a website URL, or raise ValueError. Restricts to http/https: the URL
    renders as an <a href> on the PUBLIC member page, so a `javascript:`/`data:` scheme here would
    be stored XSS. A scheme check (not a substring of "://") is the gate: `javascript://x.y//comment`
    carries "://" and a dotted host but must be rejected."""
    url = _normalize_url(url)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or "." not in parts.netloc:
        raise ValueError("website must be an http(s) URL, e.g. https://example.com")
    return url


def link_member_website(url: str, member_slug: str, *, linked_by: str | None = None,
                        is_primary: bool = False, label: str | None = None) -> None:
    url = _require_web_url(url)
    link_identity("website", url, member_slug, is_primary=is_primary, linked_by=linked_by, label=label)


def update_member_website(old_url: str, member_slug: str, *, url: str | None = None,
                          label: str | None = None) -> bool:
    """Edit one of a member's existing websites in place: rename it (set/clear the display `label`)
    and/or change its URL. Unlike `link_member_website`'s upsert (which COALESCEs the label and can
    only add), this UPDATEs the row, so the name can be cleared and the URL changed without losing
    the row's primary flag. Returns True if a row was changed (False if the old URL wasn't found).
    Raises ValueError on a bad new URL or a collision with another of the member's websites."""
    old = _normalize_url(old_url)
    new = _require_web_url(url) if url else old
    label = (label or "").strip() or None
    with connect() as conn:
        mid = _member_id_for_slug(conn, member_slug)
        if mid is None:
            return False
        try:
            cur = conn.execute(
                "UPDATE member_identities SET identifier = ?, label = ?, updated_at = ? "
                "WHERE surface = 'website' AND member_id = ? AND identifier = ?",
                (new, label, _now(), mid, old))
        except sqlite3.IntegrityError:
            raise ValueError("you already have that website")
        return cur.rowcount > 0


def websites_for_member(member_slug: str) -> list[str]:
    """All of a member's website URLs, primary first. Public — shown on the member's profile page."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT mi.identifier AS url FROM member_identities mi "
            "JOIN club_members m ON m.id = mi.member_id "
            "WHERE mi.surface = 'website' AND m.slug = ? "
            "ORDER BY mi.is_primary DESC, mi.created_at, mi.identifier",
            (member_slug,),
        ).fetchall()
    return [r["url"] for r in rows]


def list_member_websites() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT mi.identifier AS url, m.slug AS member_slug, mi.is_primary, "
            "mi.linked_by, mi.created_at, mi.updated_at "
            "FROM member_identities mi JOIN club_members m ON m.id = mi.member_id "
            "WHERE mi.surface = 'website' ORDER BY m.slug, mi.identifier"
        ).fetchall()
    return [dict(r) for r in rows]


# The first (and only) identity-removal path. Email is deliberately NOT removable: addresses anchor
# mailing-list attribution (mail_messages.member_id resolves through them), so dropping one would
# silently break who past + future list mail is attributed to.
def unlink_member_identity(surface: str, identifier: str, member_slug: str) -> bool:
    """Delete one of a member's own identities (member-scoped). Returns True if a row was removed.
    Refuses surface='email' — those can never be removed."""
    if surface == "email":
        raise ValueError("email addresses can't be removed — they anchor mailing-list attribution")
    with connect() as conn:
        mid = _member_id_for_slug(conn, member_slug)
        if mid is None:
            return False
        cur = conn.execute(
            "DELETE FROM member_identities WHERE surface = ? AND identifier = ? AND member_id = ?",
            (surface, identifier, mid),
        )
        return cur.rowcount > 0


def remove_member_website(url: str, member_slug: str) -> bool:
    return unlink_member_identity("website", _normalize_url(url), member_slug)


def remove_member_sms(number: str, member_slug: str) -> bool:
    return unlink_member_identity("sms", _normalize_phone(number), member_slug)


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


def search_mail_archive_visible(query: str, *, viewer_member_slug: str | None,
                                is_admin: bool, member_slug: str | None = None,
                                year_from: int | None = None,
                                year_to: int | None = None,
                                limit: int = 8) -> list[dict]:
    """Actor-scoped mail search for the model tool.

    Mailing-list mail is shared among linked club members.  A 1:1 row (``list_id IS NULL``) is
    visible only to the member attached to that row.  Admin callers may search the whole archive,
    but even their model-facing result omits raw email addresses; internal repair code keeps using
    ``search_mail_archive`` above when it needs those fields.
    """
    match = _fts_query(query)
    if not match or (not viewer_member_slug and not is_admin):
        return []
    if member_slug and not is_admin and member_slug != viewer_member_slug:
        return []
    limit = max(1, min(int(limit), 20))
    sql = (
        "SELECT m.message_id, m.thread_id, m.subject, m.from_name, "
        "cm.slug AS member_slug, m.sent_at, m.received_at, "
        "snippet(mail_message_fts, 4, '[', ']', ' ... ', 18) AS snippet "
        "FROM mail_message_fts JOIN mail_messages m "
        "ON m.message_id = mail_message_fts.message_id "
        "LEFT JOIN club_members cm ON cm.id = m.member_id "
        "WHERE mail_message_fts MATCH ?"
    )
    args: list = [match]
    if member_slug:
        sql += " AND m.member_id = (SELECT id FROM club_members WHERE slug = ?)"
        args.append(member_slug)
    if not is_admin:
        sql += (
            " AND (m.list_id IS NOT NULL OR "
            "m.member_id = (SELECT id FROM club_members WHERE slug = ?))"
        )
        args.append(viewer_member_slug)
    if year_from:
        sql += " AND m.sent_at >= ?"
        args.append(f"{int(year_from):04d}-01-01")
    if year_to:
        sql += " AND m.sent_at < ?"
        args.append(f"{int(year_to) + 1:04d}-01-01")
    sql += " ORDER BY COALESCE(m.sent_at, m.received_at, m.imported_at) DESC LIMIT ?"
    args.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def mail_messages_since(sent_at: str, *, exclude_from: str | None = None,
                        mailing_list_only: bool = False, limit: int = 500) -> list[dict]:
    """Member-authored archive mail newer than ``sent_at``, oldest first.

    ``mailing_list_only`` is the deterministic shared/private boundary used by reflection's club
    lane.  Only member-linked senders are returned; ``exclude_from`` drops Oliver's own outbound.
    """
    sql = (
        "SELECT m.message_id, m.subject, m.from_email, cm.slug AS member_slug, m.sent_at, "
        "m.body_clean FROM mail_messages m JOIN club_members cm ON cm.id = m.member_id "
        "WHERE m.sent_at > ?"
    )
    args: list = [sent_at]
    if mailing_list_only:
        sql += " AND m.list_id IS NOT NULL"
    if exclude_from:
        sql += " AND m.from_email != ?"; args.append(exclude_from.lower())
    sql += " ORDER BY m.sent_at ASC LIMIT ?"
    args.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def latest_mail_sent_at() -> str | None:
    """MAX(sent_at) in the archive — used to initialize the reflection mail cursor forward-only."""
    with connect() as conn:
        row = conn.execute("SELECT MAX(sent_at) AS m FROM mail_messages").fetchone()
    return row["m"] if row and row["m"] else None


def mail_messages_between(start: str, end: str, *, member_slug: str | None = None,
                          exclude_from: str | None = None, limit: int = 500) -> list[dict]:
    """Member-authored archive mail with start < sent_at <= end, oldest first — the archive
    miner's per-year feed (member-filtered for the member lane, unfiltered for the club lane)."""
    sql = (
        "SELECT m.message_id, m.subject, m.from_email, cm.slug AS member_slug, m.sent_at, "
        "m.body_clean FROM mail_messages m JOIN club_members cm ON cm.id = m.member_id "
        "WHERE m.sent_at > ? AND m.sent_at <= ?"
    )
    args: list = [start, end]
    if member_slug:
        sql += " AND cm.slug = ?"; args.append(member_slug)
    if exclude_from:
        sql += " AND m.from_email != ?"; args.append(exclude_from.lower())
    sql += " ORDER BY m.sent_at ASC LIMIT ?"
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


def get_mail_thread_visible(thread_id: str, *, viewer_member_slug: str | None,
                            is_admin: bool, limit: int = 50) -> dict | None:
    """Actor-scoped, PII-minimized thread transcript for the model tool."""
    if not viewer_member_slug and not is_admin:
        return None
    limit = max(1, min(int(limit), 100))
    with connect() as conn:
        thread = conn.execute(
            "SELECT thread_id, subject_normalized, first_sent_at, last_sent_at, "
            "message_count, summary, summary_updated_at FROM mail_threads WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        if not thread:
            return None
        sql = (
            "SELECT m.message_id, m.from_name, cm.slug AS member_slug, m.subject, "
            "m.sent_at, m.body_clean FROM mail_messages m "
            "LEFT JOIN club_members cm ON cm.id = m.member_id WHERE m.thread_id = ?"
        )
        args: list = [thread_id]
        if not is_admin:
            sql += (
                " AND (m.list_id IS NOT NULL OR "
                "m.member_id = (SELECT id FROM club_members WHERE slug = ?))"
            )
            args.append(viewer_member_slug)
        sql += " ORDER BY m.sent_at ASC, m.message_id ASC LIMIT ?"
        args.append(limit)
        rows = conn.execute(sql, args).fetchall()
    if not rows:  # indistinguishable from a nonexistent thread to an unauthorized actor
        return None
    return {"thread": dict(thread), "messages": [dict(r) for r in rows]}


# ── Conversations + rolling summary ──────────────────────────────────────────
def log_message(channel_id: str, role: str, content: str, speaker: str | None = None,
                member_slug: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO conversations (channel_id, role, speaker, content, member_slug) "
            "VALUES (?, ?, ?, ?, ?)",
            (channel_id, role, speaker, content, member_slug),
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


def conversations_after_global(after_id: int, limit: int = 2000) -> list[dict]:
    """Turns with id > after_id across ALL channels, oldest first — the reflection job's feed."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, channel_id, role, speaker, content, member_slug, created_at "
            "FROM conversations WHERE id > ? ORDER BY id ASC LIMIT ?",
            (after_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_job_state(key: str) -> dict | None:
    """A scheduled job's persisted state blob (JSON), or None if the job has never run."""
    return _jobs_repo.get_state(connect, key)


def set_job_state(key: str, value: dict) -> None:
    _jobs_repo.set_state(connect, key, value, now=_now())


def recent_messages(channel_id: str, limit: int = 12) -> list[dict]:
    """Recent Oliver-visible conversation turns for a channel, newest last."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, role, speaker, content, created_at FROM conversations "
            "WHERE channel_id = ? ORDER BY id DESC LIMIT ?",
            (channel_id, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def conversation_medium(channel_id: str) -> str:
    """Human label for the surface a logged turn came from, derived from its channel_id
    (Discord channels are numeric ids; email threads are prefixed). Used to tell Oliver — and
    the member — which medium a recalled turn happened on."""
    if (channel_id or "").startswith("email:list:"):
        return "mailing list"
    if (channel_id or "").startswith("email:"):
        return "email"
    return "Discord"


def search_conversations(query: str, *, limit: int = 12,
                         channel_ids: list[str] | None = None,
                         member_slug: str | None = None) -> list[dict]:
    """Keyword search over logged turns across ALL channels and mediums, newest first.

    Splits the query into whitespace terms; a row must contain every term (AND match). Spans every
    channel Oliver has logged — Discord AND email threads — unless narrowed by `channel_ids` or
    `member_slug` (conversations with one member, across mediums). The simple LIKE backend is
    swappable for FTS5/embeddings later without changing callers.
    """
    terms = [t for t in query.split() if t]
    if not terms:
        return []
    sql = (
        "SELECT id, channel_id, role, speaker, content, member_slug, created_at FROM conversations "
        "WHERE " + " AND ".join("content LIKE ?" for _ in terms)
    )
    args: list = [f"%{t}%" for t in terms]
    if channel_ids:
        sql += f" AND channel_id IN ({','.join('?' for _ in channel_ids)})"
        args += channel_ids
    if member_slug:
        sql += " AND member_slug = ?"
        args.append(member_slug)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def search_conversations_visible(query: str, *, viewer_member_slug: str | None,
                                 is_admin: bool, limit: int = 12,
                                 member_slug: str | None = None) -> list[dict]:
    """Actor-scoped conversation search for ``search_discussion``.

    Discord and mailing-list turns are shared with linked members.  Direct-email turns are visible
    only when tagged to the viewer.  Supplying ``member_slug`` narrows to that person's turns and is
    defensively refused for a different non-admin member even if the dispatcher missed the check.
    """
    if is_admin:
        return search_conversations(query, limit=limit, member_slug=member_slug)
    if not viewer_member_slug or (member_slug and member_slug != viewer_member_slug):
        return []
    terms = [t for t in query.split() if t]
    if not terms:
        return []
    sql = (
        "SELECT id, channel_id, role, speaker, content, member_slug, created_at FROM conversations "
        "WHERE " + " AND ".join("content LIKE ?" for _ in terms)
    )
    args: list = [f"%{t}%" for t in terms]
    if member_slug:
        # A self-scoped search may span shared channels and this member's direct email, but can
        # never select another member's rows.
        sql += " AND member_slug = ?"
        args.append(viewer_member_slug)
    else:
        sql += (
            " AND (channel_id NOT LIKE 'email:%' OR channel_id LIKE 'email:list:%' "
            "OR member_slug = ?)"
        )
        args.append(viewer_member_slug)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 20)))
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def recent_threads_for_member(member_slug: str, *, exclude_channel: str | None = None,
                              limit: int = 3) -> list[dict]:
    """Most recent conversation per OTHER channel Oliver has had with this member, newest first —
    used to proactively remind Oliver a member has a recent thread on another medium. Each entry:
    {medium, channel_id, last_at, snippet}. `exclude_channel` drops the channel currently being
    answered (Oliver already sees that one)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT channel_id, MAX(id) AS last_id FROM conversations "
            "WHERE member_slug = ? AND channel_id != ? "
            "GROUP BY channel_id ORDER BY last_id DESC LIMIT ?",
            (member_slug, exclude_channel or "", limit),
        ).fetchall()
        out = []
        for r in rows:
            turn = conn.execute(
                "SELECT content, created_at FROM conversations WHERE id = ?", (r["last_id"],)
            ).fetchone()
            out.append({
                "medium": conversation_medium(r["channel_id"]),
                "channel_id": r["channel_id"],
                "last_at": turn["created_at"] if turn else None,
                "snippet": ((turn["content"] if turn else "") or "")[:160],
            })
        return out


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


# ── Event log (the club timeline) + meeting-status projection ───────────────
# One append-only `events` log is the club's timeline; `meeting_member_status` is the current-state
# projection over the meeting_ops member events. record_event writes both atomically.
READING_STATUSES = {"not_started", "started", "on_track", "behind", "finished", "paused"}

# kind → category. Phase 1 populates meeting_ops + the meeting_scheduled hook; the Phase 2 chronicle
# kinds (the archive miner + the live recording surface) fill the rest of the taxonomy. A `record_event`
# caller may also pass `category=` explicitly to override this map (used by the free-form admin log).
_KIND_CATEGORY = {
    # ── Phase 1: meeting operations ──
    "attendance_requested": "meeting_ops", "attendance_reported": "meeting_ops",
    "reading_requested": "meeting_ops", "reading_reported": "meeting_ops",
    "roll_call_opened": "meeting_ops", "roll_call_closed": "meeting_ops",
    "attendance_alert_sent": "meeting_ops", "week_reminder_sent": "meeting_ops",
    "briefing_sent": "meeting_ops", "email_reply": "meeting_ops",
    # ── meeting lifecycle (Phase 1 hook + Phase 2 chronicle) ──
    "meeting_scheduled": "meeting", "meeting_rescheduled": "meeting",
    "meeting_canceled": "meeting", "meeting_held": "meeting", "location_set": "meeting",
    # ── book selection ──
    "book_nominated": "selection", "poll_opened": "selection",
    "vote_cast": "selection", "book_picked": "selection",
    # ── in-person social ──
    "dinner": "social", "spouses_event": "social", "hosting": "social",
    # ── member life (operational + shared milestones only) ──
    "member_joined": "member_life", "member_left": "member_life",
    "member_away": "member_life", "member_milestone": "member_life",
    # ── club / tooling milestones ──
    "website_launched": "club", "tooling_change": "club", "mailing_list_flurry": "club",
    "release_notes_sent": "club",
    # ── reading / discussion ──
    "book_discussed": "reading", "strong_opinion": "reading",
    "dnf": "reading", "award_given": "reading",
    # ── free-form admin / Oliver note (category supplied explicitly) ──
    "note": "other",
}

# The chronicle vocabulary the miner + live recording surface may emit, grouped by category, for prompt
# construction + caller-side validation. (meeting_ops kinds are written only by Phase 1 plumbing.)
CHRONICLE_KINDS: dict[str, tuple[str, ...]] = {
    "meeting": ("meeting_scheduled", "meeting_rescheduled", "meeting_canceled", "meeting_held", "location_set"),
    "selection": ("book_nominated", "poll_opened", "vote_cast", "book_picked"),
    "social": ("dinner", "spouses_event", "hosting"),
    "member_life": ("member_joined", "member_left", "member_away", "member_milestone"),
    "club": ("website_launched", "tooling_change", "mailing_list_flurry"),
    "reading": ("book_discussed", "strong_opinion", "dnf", "award_given"),
}
# member kinds whose event updates the meeting_member_status projection (require both ids)
_PROJECTION_KINDS = {
    "attendance_requested", "attendance_reported", "reading_requested", "reading_reported",
}


def _bump_projection(conn, kind: str, meeting_id: int, member_id: int, detail, now: str) -> None:
    conn.execute(
        "INSERT INTO meeting_member_status (meeting_id, member_id) VALUES (?, ?) "
        "ON CONFLICT(meeting_id, member_id) DO NOTHING",
        (meeting_id, member_id))
    if kind == "attendance_requested":
        conn.execute(
            "UPDATE meeting_member_status SET attendance_asks = attendance_asks + 1, "
            "last_asked_at = ?, updated_at = ? WHERE meeting_id = ? AND member_id = ?",
            (now, now, meeting_id, member_id))
    elif kind == "attendance_reported":
        conn.execute(
            "UPDATE meeting_member_status SET attendance = ?, attendance_answered_at = ?, "
            "updated_at = ? WHERE meeting_id = ? AND member_id = ?",
            (detail, now, now, meeting_id, member_id))
    elif kind == "reading_requested":
        conn.execute(
            "UPDATE meeting_member_status SET reading_asks = reading_asks + 1, last_asked_at = ?, "
            "reading_last_asked_at = ?, updated_at = ? WHERE meeting_id = ? AND member_id = ?",
            (now, now, now, meeting_id, member_id))
    elif kind == "reading_reported":
        d = json.loads(detail) if detail else {}
        conn.execute(
            "UPDATE meeting_member_status SET reading = ?, reading_progress = ?, reading_page = ?, "
            "reading_percent = ?, reading_answered_at = ?, updated_at = ? "
            "WHERE meeting_id = ? AND member_id = ?",
            (d.get("status"), d.get("progress"), d.get("page"), d.get("percent"), now, now,
             meeting_id, member_id))


def record_event(*, actor: str, kind: str, member_id: int | None = None,
                 meeting_id: int | None = None, detail: str | None = None,
                 surface: str | None = None, occurred_at: str | None = None,
                 source: str | None = None, category: str | None = None) -> int:
    """Append one event to the club timeline; if it's a meeting_ops member event, update the
    meeting_member_status projection in the same transaction. Returns the new event id.

    `category` is normally derived from `kind`; pass it explicitly to override (the free-form
    admin/Oliver log uses kind='note' with a chosen category)."""
    if kind in _PROJECTION_KINDS and (member_id is None or meeting_id is None):
        raise ValueError(f"{kind} requires both member_id and meeting_id")
    category = category or _KIND_CATEGORY.get(kind, "other")
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False, default=str)
    now = _now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO events (member_id, meeting_id, actor, category, kind, detail, surface, "
            "source, occurred_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (member_id, meeting_id, actor, category, kind, detail, surface, source,
             occurred_at or now, now))
        if kind in _PROJECTION_KINDS:
            _bump_projection(conn, kind, meeting_id, member_id, detail, now)
        return cur.lastrowid


def record_attendance_request(meeting_id: int, member_id: int, *, actor: str = "oliver",
                              surface: str | None = None) -> int:
    return record_event(actor=actor, kind="attendance_requested",
                        member_id=member_id, meeting_id=meeting_id, surface=surface)


def record_attendance_report(meeting_id: int, member_id: int, status: str, *,
                             actor: str = "member", surface: str | None = None,
                             updated_by: str | None = None) -> int:
    if status not in {"yes", "no", "unsure"}:
        raise ValueError("attendance status must be yes, no, or unsure")
    return record_event(actor=actor, kind="attendance_reported", member_id=member_id,
                        meeting_id=meeting_id, detail=status, surface=surface, source=updated_by)


def record_reading_request(meeting_id: int, member_id: int, *, actor: str = "oliver",
                           surface: str | None = None) -> int:
    return record_event(actor=actor, kind="reading_requested",
                        member_id=member_id, meeting_id=meeting_id, surface=surface)


def record_reading_report(meeting_id: int, member_id: int, status: str, *,
                          progress: str | None = None, page: int | None = None,
                          percent: int | None = None, actor: str = "member",
                          surface: str | None = None, updated_by: str | None = None) -> int:
    if status not in READING_STATUSES:
        raise ValueError(f"reading status must be one of {sorted(READING_STATUSES)}")
    if page is not None and page < 0:
        raise ValueError("page must be >= 0")
    if percent is not None and not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    detail = json.dumps({"status": status, "progress": progress, "page": page, "percent": percent})
    return record_event(actor=actor, kind="reading_reported", member_id=member_id,
                        meeting_id=meeting_id, detail=detail, surface=surface, source=updated_by)


def record_group_event(meeting_id: int, kind: str, *, actor: str = "oliver",
                       surface: str = "system", detail: str | None = None,
                       occurred_at: str | None = None, source: str | None = None) -> int:
    return record_event(actor=actor, kind=kind, meeting_id=meeting_id, detail=detail,
                        surface=surface, occurred_at=occurred_at, source=source)


def record_meeting_scheduled(meeting_id: int, *, detail: str | None = None,
                             occurred_at: str | None = None, actor: str = "admin") -> int:
    return record_event(actor=actor, kind="meeting_scheduled", meeting_id=meeting_id,
                        detail=detail, surface="system", occurred_at=occurred_at)


def record_release_notes_sent(commit: str, *, scope: str, subject: str,
                              window: str | None = None, release_name: str | None = None,
                              occurred_at: str | None = None) -> int:
    """Log a release-notes send to the club timeline (category 'club').

    `commit` is the repo HEAD at send time; it becomes the baseline the *next* release-notes
    scopes from (everything since this commit). The detail JSON carries the commit plus the
    human-facing subject/scope/window so the timeline reads as a real club milestone.
    `release_name` is the christened release name ("Quixotic Quicksilver") — a list send with a
    name makes that name Oliver's current release (`current_release`); pre-naming rows lack it."""
    detail = {"commit": commit, "subject": subject, "scope": scope}
    if window:
        detail["window"] = window
    if release_name:
        detail["release_name"] = release_name
    return record_event(actor="oliver", kind="release_notes_sent", category="club",
                        surface="system", detail=detail, occurred_at=occurred_at)


def last_release_notes_commit() -> str | None:
    """The HEAD commit recorded by the most recent release-notes send, or None if there's never
    been one. The next release-notes scopes from here (`<commit>..HEAD`)."""
    with connect() as conn:
        row = conn.execute(
            "SELECT detail FROM events WHERE kind = 'release_notes_sent' "
            "ORDER BY occurred_at DESC, id DESC LIMIT 1").fetchone()
    if not row or not row["detail"]:
        return None
    try:
        return json.loads(row["detail"]).get("commit")
    except (ValueError, TypeError):
        return None


def release_history() -> list[dict]:
    """Every release-notes send, newest first: {name, commit, subject, occurred_at}. `name` is
    None for releases sent before naming existed. Feeds the naming prompt (never reuse a name)
    and `current_release`."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT detail, occurred_at FROM events WHERE kind = 'release_notes_sent' "
            "ORDER BY occurred_at DESC, id DESC").fetchall()
    out = []
    for row in rows:
        try:
            detail = json.loads(row["detail"]) if row["detail"] else {}
        except (ValueError, TypeError):
            detail = {}
        out.append({"name": detail.get("release_name"), "commit": detail.get("commit"),
                    "subject": detail.get("subject"), "occurred_at": row["occurred_at"]})
    return out


def current_release() -> dict | None:
    """The newest NAMED release — what release of his own software Oliver is running. None until
    the first named release ships (unnamed pre-naming sends never win)."""
    return next((r for r in release_history() if r["name"]), None)


def meeting_member_status_for_meeting(meeting_id: int) -> list[dict]:
    """Current status rows for a meeting, with member slug/name projected. Sparse — a member with
    no row is 'unknown'/'unknown' (callers default that)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT s.*, m.slug AS member_slug, m.name AS member_name "
            "FROM meeting_member_status s JOIN club_members m ON m.id = s.member_id "
            "WHERE s.meeting_id = ? ORDER BY m.name", (meeting_id,)).fetchall()
    return [dict(r) for r in rows]


def meeting_member_status(meeting_id: int, member_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT s.*, m.slug AS member_slug, m.name AS member_name "
            "FROM meeting_member_status s JOIN club_members m ON m.id = s.member_id "
            "WHERE s.meeting_id = ? AND s.member_id = ?", (meeting_id, member_id)).fetchone()
    return dict(row) if row else None


def events_for_member(member_id: int, *, limit: int = 100) -> list[dict]:
    """A member's timeline across all meetings + non-meeting events, newest first."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE member_id = ? ORDER BY occurred_at DESC, id DESC LIMIT ?",
            (member_id, limit)).fetchall()
    return [dict(r) for r in rows]


def meeting_events(meeting_id: int, *, member_id: int | None = None, kind: str | None = None,
                   limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM events WHERE meeting_id = ?"
    args: list = [meeting_id]
    if member_id is not None:
        sql += " AND member_id = ?"; args.append(member_id)
    if kind is not None:
        sql += " AND kind = ?"; args.append(kind)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"; args.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def current_roll_call(meeting_id: int) -> dict | None:
    """The meeting's roll-call state, derived from the latest roll_call_opened/closed events —
    reproduces the old get_roll_call shape ({meeting_id, channel_id, message_id, status, opened_by,
    opened_at, closed_at}). None if a roll call was never opened."""
    with connect() as conn:
        opened = conn.execute(
            "SELECT detail, created_at FROM events WHERE meeting_id = ? AND kind = 'roll_call_opened' "
            "ORDER BY created_at DESC, id DESC LIMIT 1", (meeting_id,)).fetchone()
        if not opened:
            return None
        closed = conn.execute(
            "SELECT created_at FROM events WHERE meeting_id = ? AND kind = 'roll_call_closed' "
            "ORDER BY created_at DESC, id DESC LIMIT 1", (meeting_id,)).fetchone()
    d = json.loads(opened["detail"]) if opened["detail"] else {}
    is_closed = bool(closed and closed["created_at"] >= opened["created_at"])
    return {
        "meeting_id": meeting_id,
        "channel_id": d.get("channel_id"),
        "message_id": d.get("message_id"),
        "opened_by": d.get("opened_by"),
        "status": "closed" if is_closed else "open",
        "opened_at": opened["created_at"],
        "closed_at": closed["created_at"] if is_closed else None,
    }


def has_open_roll_call(meeting_id: int) -> bool:
    rc = current_roll_call(meeting_id)
    return bool(rc and rc["status"] == "open")


def has_group_event(meeting_id: int, kind: str) -> bool:
    """Whether a group (member-less) event of this kind exists for the meeting — replaces the
    cadence dedup keys (week_reminder_sent / briefing_sent / attendance_alert_sent / …)."""
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM events WHERE meeting_id = ? AND member_id IS NULL AND kind = ? LIMIT 1",
            (meeting_id, kind)).fetchone() is not None


def recent_group_event_details(kind: str, *, limit: int = 3) -> list[str | None]:
    """The `detail` payloads of the most recent group (member-less) events of this kind, newest
    first — for cross-edition rotation (e.g. which books recent Postscripts already featured)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT detail FROM events WHERE member_id IS NULL AND kind = ? ORDER BY id DESC LIMIT ?",
            (kind, limit)).fetchall()
    return [r["detail"] for r in rows]


def event_source_exists(source: str) -> bool:
    """Whether any event already carries this provenance string — the idempotency guard for the
    archive miner's loader (source = 'mail:<thread_id>#<n>'), so re-loads don't duplicate."""
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM events WHERE source = ? LIMIT 1", (source,)).fetchone() is not None


def timeline(*, category: str | None = None, member_id: int | None = None,
             since: str | None = None, until: str | None = None, limit: int = 50) -> list[dict]:
    """The club-wide timeline (any member, any/no meeting), newest first — the general read behind
    the `club_timeline` tool. Filter by category, member, and/or an occurred_at date window."""
    sql = "SELECT e.*, m.slug AS member_slug, m.name AS member_name FROM events e " \
          "LEFT JOIN club_members m ON m.id = e.member_id WHERE 1=1"
    args: list = []
    if category is not None:
        sql += " AND e.category = ?"; args.append(category)
    if member_id is not None:
        sql += " AND e.member_id = ?"; args.append(member_id)
    if since is not None:
        sql += " AND e.occurred_at >= ?"; args.append(since)
    if until is not None:
        sql += " AND e.occurred_at <= ?"; args.append(until)
    sql += " ORDER BY e.occurred_at DESC, e.id DESC LIMIT ?"; args.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def delete_event(event_id: int) -> bool:
    """Delete one event from the club timeline by id (the admin events view's selective delete).
    Returns True if a row was removed. Note: this does NOT recompute the meeting_member_status
    projection, so it's intended for chronicle/club/note rows, not live meeting-ops bookkeeping."""
    with connect() as conn:
        return conn.execute("DELETE FROM events WHERE id = ?", (event_id,)).rowcount > 0


def list_mail_threads(*, limit: int | None = None) -> list[dict]:
    """Every archived mail thread, oldest-first — the spine the archive miner walks. Returns
    {thread_id, subject, first_sent_at, last_sent_at, message_count}."""
    sql = ("SELECT thread_id, subject_normalized AS subject, first_sent_at, last_sent_at, "
           "message_count FROM mail_threads ORDER BY first_sent_at ASC, thread_id ASC")
    args: list = []
    if limit is not None:
        sql += " LIMIT ?"; args.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


# ── Oliver action proposals ─────────────────────────────────────────────────
# ── Review drafts (the review-drive email state machine) ─────────────────────
def create_review_draft(*, member_id: int, book_slug: str, thread_id: str | None,
                        draft_json: str | None = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO review_drafts (member_id, book_slug, thread_id, draft_json) "
            "VALUES (?, ?, ?, ?)",
            (member_id, book_slug, thread_id, draft_json))
        return cur.lastrowid


def draft_for_thread(thread_id: str | None) -> dict | None:
    """The OPEN draft (awaiting reply/confirm) on this email thread, if any — the router's key."""
    if not thread_id:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM review_drafts WHERE thread_id = ? "
            "AND state IN ('awaiting_reply', 'awaiting_confirm') "
            "ORDER BY id DESC LIMIT 1", (thread_id,)).fetchone()
    return dict(row) if row else None


def open_draft_for_member(member_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM review_drafts WHERE member_id = ? "
            "AND state IN ('awaiting_reply', 'awaiting_confirm') "
            "ORDER BY id DESC LIMIT 1", (member_id,)).fetchone()
    return dict(row) if row else None


def update_review_draft(draft_id: int, *, state: str | None = None,
                        draft_json: str | None = None, rounds: int | None = None,
                        thread_id: str | None = None) -> None:
    sets, args = ["updated_at = datetime('now')"], []
    for col, val in (("state", state), ("draft_json", draft_json),
                     ("rounds", rounds), ("thread_id", thread_id)):
        if val is not None:
            sets.append(f"{col} = ?")
            args.append(val)
    with connect() as conn:
        conn.execute(f"UPDATE review_drafts SET {', '.join(sets)} WHERE id = ?",
                     (*args, draft_id))


def expire_stale_review_drafts(days: int) -> int:
    """Expire open drafts older than `days`. An ignored ask must not block a member's future
    asks forever — expiry frees them for the cadence (the per-book ask cap still applies, so
    an expired book gets at most one more try ever). Returns the number expired."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE review_drafts SET state = 'expired', updated_at = datetime('now') "
            "WHERE state IN ('awaiting_reply', 'awaiting_confirm') "
            "AND created_at < datetime('now', ?)", (f"-{days} days",))
        return cur.rowcount


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


# ── Durable outbound effects ─────────────────────────────────────────────────
def enqueue_outbox(*, idempotency_key: str, kind: str, payload_json: str,
                   max_attempts: int = 5) -> dict:
    return _outbox_repo.enqueue(
        connect, now=_now(), idempotency_key=idempotency_key, kind=kind,
        payload_json=payload_json, max_attempts=max_attempts,
    )


def outbox_by_key(idempotency_key: str) -> dict | None:
    return _outbox_repo.by_key(connect, idempotency_key)


def pending_outbox(*, limit: int = 20, now: str | None = None) -> list[dict]:
    return _outbox_repo.pending(connect, limit=limit, now=now or _now())


def claim_outbox(idempotency_key: str, *, worker_id: str, lease_expires_at: str,
                 now: str | None = None) -> dict | None:
    return _outbox_repo.claim(
        connect, idempotency_key, worker_id=worker_id,
        lease_expires_at=lease_expires_at, now=now or _now(),
    )


def mark_outbox_delivering(outbox_id: int, *, worker_id: str, now: str | None = None) -> bool:
    return _outbox_repo.mark_delivering(
        connect, outbox_id, worker_id=worker_id, now=now or _now()
    )


def mark_outbox_delivered(outbox_id: int, *, worker_id: str, provider_ref_json: str,
                          now: str | None = None) -> bool:
    return _outbox_repo.mark_delivered(
        connect, outbox_id, worker_id=worker_id,
        provider_ref_json=provider_ref_json, now=now or _now(),
    )


def mark_outbox_retry(outbox_id: int, *, worker_id: str, error: str, available_at: str,
                      now: str | None = None) -> str | None:
    return _outbox_repo.mark_retry(
        connect, outbox_id, worker_id=worker_id, error=error,
        available_at=available_at, now=now or _now(),
    )


def mark_outbox_uncertain(outbox_id: int, *, worker_id: str, error: str,
                          now: str | None = None) -> bool:
    return _outbox_repo.mark_uncertain(
        connect, outbox_id, worker_id=worker_id, error=error, now=now or _now()
    )


def recover_expired_outbox(*, now: str | None = None) -> dict:
    """Recover safe pre-send claims and quarantine ambiguous in-flight deliveries."""
    return _outbox_repo.recover_expired(connect, now=now or _now())


def outbox_status_counts() -> dict[str, int]:
    return _outbox_repo.status_counts(connect)


# ── Scheduled-job leases and run ledger ─────────────────────────────────────
def begin_job_run(job_name: str, *, lease_owner: str, lease_expires_at: str,
                  expected_interval_seconds: int, now: str | None = None) -> dict | None:
    """Atomically acquire a job lease and open its run row; None means another owner is active."""
    return _jobs_repo.begin_run(
        connect, job_name, lease_owner=lease_owner, lease_expires_at=lease_expires_at,
        expected_interval_seconds=expected_interval_seconds, now=now or _now(),
    )


def renew_job_lease(job_name: str, *, lease_owner: str, lease_expires_at: str,
                    now: str | None = None) -> bool:
    return _jobs_repo.renew_lease(
        connect, job_name, lease_owner=lease_owner,
        lease_expires_at=lease_expires_at, now=now or _now(),
    )


def finish_job_run(run_id: int, *, job_name: str, lease_owner: str, outcome: str,
                   duration_ms: int, processed_count: int = 0, error: str | None = None,
                   now: str | None = None) -> bool:
    return _jobs_repo.finish_run(
        connect, run_id, job_name=job_name, lease_owner=lease_owner, outcome=outcome,
        duration_ms=duration_ms, processed_count=processed_count, error=error,
        now=now or _now(),
    )


def job_run(run_id: int) -> dict | None:
    return _jobs_repo.run_by_id(connect, run_id)


def job_statuses(*, now: str | None = None) -> list[dict]:
    """Operational-only job status. Contains names/timestamps/error classes, never job payloads."""
    return _jobs_repo.statuses(connect, now=now or _now())
