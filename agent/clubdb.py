"""Authoritative club record — the relational source of truth (class A, in SQLite).

This is the inversion: the club's books / meetings / members / authors / reviews /
awards live here under integer primary keys and real foreign keys. The Git corpus
(``corpus/data/*``) and the 11ty website are *generated* from these tables (see
``agent.corpus_gen``); Airtable is retired after the one-time import (see
``agent.script.import_airtable``).

Identity is an integer surrogate key everywhere — never a slug. The integer ids are
Airtable's own autonumbers (Book ID / Meeting ID / Member ID / Author ID / Review ID);
awards have no Airtable autonumber so they mint one. ``slug`` is kept only as the
generated corpus filename stem (a ``UNIQUE`` output column), never as identity.

These tables are additive and created idempotently (CREATE TABLE IF NOT EXISTS),
alongside Oliver's existing operational tables in the same ``oliver.db`` file. Reusing
``db.connect()`` means they share the WAL connection settings and ``foreign_keys=ON``.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Iterator

from agent import db
from corpus.paths import slugify

# ── Schema ───────────────────────────────────────────────────────────────────
# Prefixed ``club_`` so they never collide with the operational ``member_*`` /
# ``mail_*`` tables and read unambiguously as the authoritative club record.
CLUB_SCHEMA = """
CREATE TABLE IF NOT EXISTS club_members (
    id          INTEGER PRIMARY KEY,        -- Airtable Member ID
    slug        TEXT NOT NULL UNIQUE,       -- corpus filename stem (output only)
    name        TEXT NOT NULL,
    is_current  INTEGER NOT NULL DEFAULT 0,
    website     TEXT,
    email       TEXT,                       -- recovered from Airtable (corpus dropped it)
    mobile      TEXT
);

CREATE TABLE IF NOT EXISTS club_authors (
    id    INTEGER PRIMARY KEY,              -- Airtable Author ID
    slug  TEXT NOT NULL UNIQUE,             -- corpus filename stem (output only)
    name  TEXT NOT NULL,
    bio   TEXT
);

CREATE TABLE IF NOT EXISTS club_books (
    id               INTEGER PRIMARY KEY,   -- Airtable Book ID
    slug             TEXT NOT NULL UNIQUE,  -- corpus filename stem (output only)
    title            TEXT NOT NULL,
    subtitle         TEXT,
    topic            TEXT,
    fiction          INTEGER NOT NULL DEFAULT 0,
    publication_year INTEGER,
    page_count       INTEGER,
    isbn13           TEXT,
    ol_key           TEXT,
    synopsis         TEXT,
    subjects_json    TEXT                   -- JSON array of OL subject tags
);

CREATE TABLE IF NOT EXISTS club_book_authors (
    book_id   INTEGER NOT NULL REFERENCES club_books(id) ON DELETE CASCADE,
    author_id INTEGER NOT NULL REFERENCES club_authors(id) ON DELETE CASCADE,
    ordinal   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (book_id, author_id)
);
CREATE INDEX IF NOT EXISTS idx_club_book_authors_author ON club_book_authors(author_id);

CREATE TABLE IF NOT EXISTS club_meetings (
    id          INTEGER PRIMARY KEY,        -- Airtable Meeting ID
    date        TEXT,                       -- full ISO datetime, verbatim from Airtable
    type_json   TEXT,                       -- JSON array of meeting types
    location    TEXT,
    notes       TEXT,
    placeholder INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS club_meeting_books (
    meeting_id INTEGER NOT NULL REFERENCES club_meetings(id) ON DELETE CASCADE,
    book_id    INTEGER NOT NULL REFERENCES club_books(id) ON DELETE CASCADE,
    ordinal    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (meeting_id, book_id)
);
CREATE INDEX IF NOT EXISTS idx_club_meeting_books_book ON club_meeting_books(book_id);

-- Book-level picker (Airtable Books.Picked by) — the canonical picker; generates
-- the corpus book.picker[]. Kept distinct from meeting host below.
CREATE TABLE IF NOT EXISTS club_book_pickers (
    book_id   INTEGER NOT NULL REFERENCES club_books(id) ON DELETE CASCADE,
    member_id INTEGER NOT NULL REFERENCES club_members(id) ON DELETE CASCADE,
    ordinal   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (book_id, member_id)
);
CREATE INDEX IF NOT EXISTS idx_club_book_pickers_member ON club_book_pickers(member_id);

-- Meeting-level host (Airtable Meetings.Host) — who hosted/ran a given meeting.
-- The correct grain for the future picking-rotation feature; not the same as the
-- book-level picker (a book can span two host meetings).
CREATE TABLE IF NOT EXISTS club_meeting_hosts (
    meeting_id INTEGER NOT NULL REFERENCES club_meetings(id) ON DELETE CASCADE,
    member_id  INTEGER NOT NULL REFERENCES club_members(id) ON DELETE CASCADE,
    ordinal    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (meeting_id, member_id)
);
CREATE INDEX IF NOT EXISTS idx_club_meeting_hosts_member ON club_meeting_hosts(member_id);

CREATE TABLE IF NOT EXISTS club_reviews (
    id                 INTEGER PRIMARY KEY,  -- Airtable Review ID
    airtable_id        TEXT,                 -- Airtable record string (rec...), for traceability
    book_id            INTEGER NOT NULL REFERENCES club_books(id) ON DELETE CASCADE,
    member_id          INTEGER NOT NULL REFERENCES club_members(id) ON DELETE CASCADE,
    rating             INTEGER,
    dnf                INTEGER NOT NULL DEFAULT 0,
    discussion_quality INTEGER,
    would_recommend    INTEGER NOT NULL DEFAULT 0,
    favorite_quote     TEXT,
    body               TEXT,
    created_at         TEXT,
    UNIQUE (book_id, member_id)
);
CREATE INDEX IF NOT EXISTS idx_club_reviews_book ON club_reviews(book_id);
CREATE INDEX IF NOT EXISTS idx_club_reviews_member ON club_reviews(member_id);

CREATE TABLE IF NOT EXISTS club_awards (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,  -- minted (no Airtable autonumber)
    name           TEXT,
    year           INTEGER,
    award_category TEXT,                    -- the Airtable single-select 'Award'
    notes          TEXT
);

CREATE TABLE IF NOT EXISTS club_award_books (
    award_id INTEGER NOT NULL REFERENCES club_awards(id) ON DELETE CASCADE,
    book_id  INTEGER NOT NULL REFERENCES club_books(id) ON DELETE CASCADE,
    PRIMARY KEY (award_id, book_id)
);

CREATE TABLE IF NOT EXISTS club_award_voters (
    award_id  INTEGER NOT NULL REFERENCES club_awards(id) ON DELETE CASCADE,
    member_id INTEGER NOT NULL REFERENCES club_members(id) ON DELETE CASCADE,
    PRIMARY KEY (award_id, member_id)
);
"""

# All club tables, for count/validation helpers and teardown in tests.
CLUB_TABLES = [
    "club_members", "club_authors", "club_books", "club_book_authors",
    "club_meetings", "club_meeting_books", "club_book_pickers", "club_meeting_hosts",
    "club_reviews", "club_awards", "club_award_books", "club_award_voters",
]


def ensure_schema() -> None:
    """Create the club-record tables idempotently. Safe to call repeatedly."""
    with db.connect() as conn:
        conn.executescript(CLUB_SCHEMA)


# ── Connection helper ────────────────────────────────────────────────────────
def connect() -> Iterator[sqlite3.Connection]:
    """Re-export db.connect so callers needn't import both modules."""
    return db.connect()


# ── Small read helpers (used by the generator and future corpus_read) ─────────
def counts() -> dict[str, int]:
    with db.connect() as conn:
        return {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] for t in CLUB_TABLES}


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args)]


def all_books(conn: sqlite3.Connection) -> list[dict]:
    """Every book with its joined authors (names, ordered), picker member slugs
    (ordered, de-duplicated by PK), and subjects — the shape the corpus generator
    needs. One pass, no N+1."""
    books = _rows(conn, "SELECT * FROM club_books ORDER BY id")
    authors_by_book: dict[int, list[str]] = {}
    for r in conn.execute(
        "SELECT ba.book_id, a.name FROM club_book_authors ba "
        "JOIN club_authors a ON a.id = ba.author_id ORDER BY ba.book_id, ba.ordinal"
    ):
        authors_by_book.setdefault(r["book_id"], []).append(r["name"])
    pickers_by_book: dict[int, list[str]] = {}
    for r in conn.execute(
        "SELECT bp.book_id, m.slug FROM club_book_pickers bp "
        "JOIN club_members m ON m.id = bp.member_id ORDER BY bp.book_id, bp.ordinal"
    ):
        pickers_by_book.setdefault(r["book_id"], []).append(r["slug"])
    for b in books:
        b["author_names"] = authors_by_book.get(b["id"], [])
        b["picker_slugs"] = pickers_by_book.get(b["id"], [])
        b["subjects"] = json.loads(b["subjects_json"]) if b["subjects_json"] else []
    return books


def all_meetings(conn: sqlite3.Connection) -> list[dict]:
    meetings = _rows(conn, "SELECT * FROM club_meetings ORDER BY id")
    books_by_meeting: dict[int, list[str]] = {}
    for r in conn.execute(
        "SELECT mb.meeting_id, b.slug FROM club_meeting_books mb "
        "JOIN club_books b ON b.id = mb.book_id ORDER BY mb.meeting_id, mb.ordinal"
    ):
        books_by_meeting.setdefault(r["meeting_id"], []).append(r["slug"])
    for m in meetings:
        m["book_slugs"] = books_by_meeting.get(m["id"], [])
        m["type"] = json.loads(m["type_json"]) if m["type_json"] else []
    return meetings


def all_members(conn: sqlite3.Connection) -> list[dict]:
    return _rows(conn, "SELECT * FROM club_members ORDER BY id")


def all_authors(conn: sqlite3.Connection) -> list[dict]:
    return _rows(conn, "SELECT * FROM club_authors ORDER BY id")


def all_reviews(conn: sqlite3.Connection) -> list[dict]:
    """Reviews joined to book + member slugs, for corpus markdown generation."""
    return _rows(
        conn,
        "SELECT r.*, b.slug AS book_slug, m.slug AS member_slug "
        "FROM club_reviews r JOIN club_books b ON b.id = r.book_id "
        "JOIN club_members m ON m.id = r.member_id ORDER BY r.created_at, r.id",
    )


def all_awards(conn: sqlite3.Connection) -> list[dict]:
    awards = _rows(conn, "SELECT * FROM club_awards ORDER BY year, id")
    books_by_award: dict[int, list[str]] = {}
    for r in conn.execute(
        "SELECT ab.award_id, b.slug FROM club_award_books ab "
        "JOIN club_books b ON b.id = ab.book_id ORDER BY ab.award_id, b.slug"
    ):
        books_by_award.setdefault(r["award_id"], []).append(r["slug"])
    voters_by_award: dict[int, list[str]] = {}
    for r in conn.execute(
        "SELECT av.award_id, m.slug FROM club_award_voters av "
        "JOIN club_members m ON m.id = av.member_id ORDER BY av.award_id, m.slug"
    ):
        voters_by_award.setdefault(r["award_id"], []).append(r["slug"])
    for a in awards:
        a["book_slugs"] = books_by_award.get(a["id"], [])
        a["voter_slugs"] = voters_by_award.get(a["id"], [])
    return awards


# ── Writes (Oliver manages the DB; corpus is regenerated after) ──────────────
def _next_id(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COALESCE(MAX(id), 0) + 1 AS n FROM {table}").fetchone()["n"]


def _author_id(conn: sqlite3.Connection, name: str) -> int:
    """Find an author by exact name, or mint a new club_authors row."""
    row = conn.execute("SELECT id FROM club_authors WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    aid = _next_id(conn, "club_authors")
    conn.execute(
        "INSERT INTO club_authors(id, slug, name, bio) VALUES (?, ?, ?, NULL)",
        (aid, slugify(name), name),
    )
    return aid


def upsert_book(conn: sqlite3.Connection, meta: dict) -> dict:
    """Insert or update a book (by slug) under FKs. Rebuilds its author links.
    Returns {id, slug, existed, author_ids}. FK integrity is enforced by the DB."""
    title = (meta.get("title") or "").strip()
    if not title:
        raise ValueError("a book needs a title")
    slug = slugify(title)
    existing = conn.execute("SELECT id FROM club_books WHERE slug = ?", (slug,)).fetchone()
    book_id = existing["id"] if existing else (meta.get("bookId") or _next_id(conn, "club_books"))
    subjects = meta.get("subjects")
    subjects_json = json.dumps(subjects, ensure_ascii=False) if subjects is not None else None
    conn.execute(
        "INSERT INTO club_books(id, slug, title, subtitle, topic, fiction, publication_year, "
        "page_count, isbn13, ol_key, synopsis, subjects_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET slug=excluded.slug, title=excluded.title, "
        "subtitle=excluded.subtitle, topic=excluded.topic, fiction=excluded.fiction, "
        "publication_year=excluded.publication_year, page_count=excluded.page_count, "
        "isbn13=excluded.isbn13, ol_key=excluded.ol_key, synopsis=excluded.synopsis, "
        "subjects_json=excluded.subjects_json",
        (book_id, slug, title, meta.get("subtitle"), meta.get("topic"),
         1 if meta.get("fiction") else 0, meta.get("publicationYear"), meta.get("pageCount"),
         meta.get("isbn13"), meta.get("olKey"), meta.get("synopsis"), subjects_json),
    )
    author_ids = [_author_id(conn, a) for a in (meta.get("authors") or []) if a]
    conn.execute("DELETE FROM club_book_authors WHERE book_id = ?", (book_id,))
    conn.executemany(
        "INSERT INTO club_book_authors(book_id, author_id, ordinal) VALUES (?, ?, ?)",
        [(book_id, aid, i) for i, aid in enumerate(author_ids)],
    )
    return {"id": book_id, "slug": slug, "existed": bool(existing), "author_ids": author_ids}


def set_book_picker(conn: sqlite3.Connection, book_id: int, member_id: int) -> None:
    conn.execute("DELETE FROM club_book_pickers WHERE book_id = ?", (book_id,))
    conn.execute(
        "INSERT INTO club_book_pickers(book_id, member_id, ordinal) VALUES (?, ?, 0)",
        (book_id, member_id),
    )


def create_meeting(conn: sqlite3.Connection, *, date_iso: str, book_id: int,
                   types: list[str] | None = None, placeholder: bool = True) -> int:
    mid = _next_id(conn, "club_meetings")
    conn.execute(
        "INSERT INTO club_meetings(id, date, type_json, location, notes, placeholder) "
        "VALUES (?, ?, ?, NULL, NULL, ?)",
        (mid, date_iso, json.dumps(types or ["Book"]), 1 if placeholder else 0),
    )
    conn.execute(
        "INSERT INTO club_meeting_books(meeting_id, book_id, ordinal) VALUES (?, ?, 0)",
        (mid, book_id),
    )
    return mid


def book_id_for_slug(conn: sqlite3.Connection, slug: str) -> int | None:
    row = conn.execute("SELECT id FROM club_books WHERE slug = ?", (slug,)).fetchone()
    return row["id"] if row else None


def member_id_for_slug(conn: sqlite3.Connection, slug: str) -> int | None:
    row = conn.execute("SELECT id FROM club_members WHERE slug = ?", (slug,)).fetchone()
    return row["id"] if row else None


# ── Module-level convenience lookups (own connection) for the ops/meeting layer ──
def lookup_member_id(slug: str | None) -> int | None:
    if not slug:
        return None
    with db.connect() as conn:
        return member_id_for_slug(conn, slug)


def meeting_id_for_book_slug(slug: str | None) -> int | None:
    """The meeting a book slug refers to (its latest, if it spanned two)."""
    if not slug:
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT MAX(mb.meeting_id) AS mid FROM club_books b "
            "JOIN club_meeting_books mb ON mb.book_id = b.id WHERE b.slug = ?",
            (slug,),
        ).fetchone()
    return row["mid"] if row and row["mid"] is not None else None


def picker_ids_for_book_slug(slug: str | None) -> list[int]:
    if not slug:
        return []
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT bp.member_id FROM club_books b "
            "JOIN club_book_pickers bp ON bp.book_id = b.id WHERE b.slug = ? ORDER BY bp.ordinal",
            (slug,),
        ).fetchall()
    return [r["member_id"] for r in rows]


def current_members() -> list[dict]:
    """Current members from the authoritative table, id-native (id, slug, name)."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, slug, name FROM club_members WHERE is_current = 1 ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    ensure_schema()
    print(json.dumps(counts(), indent=2))
