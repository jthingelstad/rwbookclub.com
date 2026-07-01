"""Authoritative club record — the relational source of truth (class A, in SQLite).

This is the inversion: the club's books / meetings / members / authors / reviews /
lists live here under integer primary keys and real foreign keys. The corpus
(``corpus/data/*``, gitignored) and the 11ty website are *generated* from these tables (see
``agent.corpus_gen``); Airtable is retired after the one-time import (see
``agent.script.import_airtable``).

Identity is an integer surrogate key everywhere — never a slug. The integer ids are
Airtable's own autonumbers (Book ID / Meeting ID / Member ID / Author ID / Review ID);
lists have no Airtable autonumber so they mint one. ``slug`` is kept only as the
generated corpus filename stem (a ``UNIQUE`` output column), never as identity.

These tables are additive and created idempotently (CREATE TABLE IF NOT EXISTS),
alongside Oliver's existing operational tables in the same ``oliver.db`` file. Reusing
``db.connect()`` means they share the WAL connection settings and ``foreign_keys=ON``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
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
    is_current  INTEGER NOT NULL DEFAULT 0
    -- contact + web addresses live in member_identities (surface='email'|'sms'|'website'); see db.py
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
    date        TEXT,                       -- LOCAL meeting date 'YYYY-MM-DD' (America/Chicago)
    start_time  TEXT,                       -- LOCAL start time 'HH:MM' (America/Chicago), NULL if TBD
    type_json   TEXT,                       -- JSON array of meeting types
    location    TEXT,                       -- host-set venue (free text)
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
    id                 INTEGER PRIMARY KEY,  -- the review's stable id (the corpus `id`; minted via _next_id)
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

-- Book lists — named, described collections of books. Member-owned by default; club lists
-- (owner_id NULL, scope='club') are admin-curated. Public: rendered on member profiles and on
-- /lists/<slug>/ pages. Replaced the old club_awards feature.
CREATE TABLE IF NOT EXISTS club_lists (
    id          INTEGER PRIMARY KEY,         -- minted via _next_id
    slug        TEXT NOT NULL UNIQUE,        -- corpus filename stem; globally unique
    name        TEXT NOT NULL,
    scope       TEXT NOT NULL,               -- 'member' | 'club'
    owner_id    INTEGER REFERENCES club_members(id) ON DELETE CASCADE,  -- NULL for club lists
    description TEXT,
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_club_lists_owner ON club_lists(owner_id);

CREATE TABLE IF NOT EXISTS club_list_books (
    list_id INTEGER NOT NULL REFERENCES club_lists(id) ON DELETE CASCADE,
    book_id INTEGER NOT NULL REFERENCES club_books(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL DEFAULT 0,
    note    TEXT,                            -- optional per-book blurb
    PRIMARY KEY (list_id, book_id)
);
CREATE INDEX IF NOT EXISTS idx_club_list_books_book ON club_list_books(book_id);

-- ── External enrichment (the clarity line) ───────────────────────────────────
-- 1:1 sidecars owned exclusively by the enrichment loop (agent.enrich). The core
-- club_* tables above are the club's curated, member-facing record; everything
-- derived from Open Library / Wikidata / Wikipedia lives here, so the loop never
-- writes core and can't clobber curated data. Regenerable: DELETE + re-run.
-- The read layer (all_books/all_authors) COALESCEs the dual-source mirror columns
-- (synopsis/publication_year/page_count/isbn13/subjects_json/bio) core-first.
CREATE TABLE IF NOT EXISTS club_book_enrichment (
    book_id          INTEGER PRIMARY KEY REFERENCES club_books(id) ON DELETE CASCADE,
    ol_cover_id      INTEGER,               -- OL cover id → covers.openlibrary.org
    edition_count    INTEGER,
    languages_json   TEXT,                  -- JSON array of edition languages
    ratings_average  REAL,
    ratings_count    INTEGER,
    wikidata_id      TEXT,                  -- Q-id
    wikipedia_url    TEXT,
    goodreads_id     TEXT,
    series           TEXT,
    awards_json      TEXT,                  -- JSON array of literary awards (Wikidata P166)
    -- gap-fill mirrors of dual-source OL fields (curated core wins on read):
    ol_key           TEXT,
    synopsis         TEXT,
    publication_year INTEGER,
    page_count       INTEGER,
    isbn13           TEXT,
    subjects_json    TEXT,
    enriched_at      TEXT,                  -- ISO timestamp of last enrichment pass
    enrichment_json  TEXT                   -- raw payloads + which source filled what
);

CREATE TABLE IF NOT EXISTS club_author_enrichment (
    author_id          INTEGER PRIMARY KEY REFERENCES club_authors(id) ON DELETE CASCADE,
    bio                TEXT,                -- gap-fill mirror of club_authors.bio
    birth_year         INTEGER,
    death_year         INTEGER,
    nationality        TEXT,
    ol_author_key      TEXT,                -- /authors/OL..A
    wikidata_id        TEXT,                -- Q-id
    wikipedia_url      TEXT,
    website            TEXT,
    notable_works_json TEXT,                -- JSON array of notable work titles
    photo_credit       TEXT,               -- image source / attribution
    enriched_at        TEXT,
    enrichment_json    TEXT
);
"""

# All club tables, for count/validation helpers and teardown in tests.
CLUB_TABLES = [
    "club_members", "club_authors", "club_books", "club_book_authors",
    "club_meetings", "club_meeting_books", "club_book_pickers", "club_meeting_hosts",
    "club_reviews", "club_lists", "club_list_books",
    # Enrichment sidecars last → reversed() deletes them before their parent tables.
    "club_book_enrichment", "club_author_enrichment",
]


MEETING_TZ = "America/Chicago"  # the club's single timezone (Minneapolis)


def _migrate_club(conn: sqlite3.Connection) -> None:
    """Additive club-schema migrations (idempotent).

    Runs on every `ensure_schema()`. Steps 2 and 3 are one-time data migrations retained only
    for long-lived DBs that predate them; for a freshly seeded DB they are no-ops — the local
    date normalization finds nothing to convert, and `import_airtable` already seeds
    club_meeting_hosts directly, so the host backfill touches no rows. Cheap and idempotent,
    so they stay rather than being gated behind a version flag.

    1. Add club_meetings.start_time if missing.
    2. Normalize club_meetings.date from the legacy UTC ISO datetime to the club's LOCAL
       date + start_time (America/Chicago). The old import stored the Airtable UTC instant,
       which displays the wrong day for evening meetings (6-7pm local rolls past midnight
       UTC in winter). Converting UTC -> local recovers the true local date and time
       (verified to match Airtable's 'Formatted Meeting Time' for all rows). Idempotent:
       once converted, `date` is 'YYYY-MM-DD' (no 'T') and is skipped.
    3. Backfill hosts for meetings that have a book but no host, using the book's picker(s):
       the host is normally the picker of the book discussed, so this is a correct fill, not a
       guess. Idempotent (only meetings with zero host rows are touched). Bookless social/
       picking meetings have no picker and are correctly left host-less.
    """
    import datetime
    from zoneinfo import ZoneInfo

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(club_meetings)")}
    if "start_time" not in cols:
        conn.execute("ALTER TABLE club_meetings ADD COLUMN start_time TEXT")

    chi = ZoneInfo(MEETING_TZ)
    rows = conn.execute(
        "SELECT id, date FROM club_meetings WHERE date LIKE '%T%'"
    ).fetchall()
    for r in rows:
        local = datetime.datetime.fromisoformat(r["date"].replace("Z", "+00:00")).astimezone(chi)
        conn.execute(
            "UPDATE club_meetings SET date = ?, start_time = ? WHERE id = ?",
            (local.strftime("%Y-%m-%d"), local.strftime("%H:%M"), r["id"]),
        )

    for r in conn.execute(
        "SELECT DISTINCT meeting_id FROM club_meeting_books "
        "WHERE meeting_id NOT IN (SELECT meeting_id FROM club_meeting_hosts)"
    ).fetchall():
        pickers = conn.execute(
            "SELECT DISTINCT bp.member_id FROM club_meeting_books mb "
            "JOIN club_book_pickers bp ON bp.book_id = mb.book_id "
            "WHERE mb.meeting_id = ? ORDER BY bp.member_id",
            (r["meeting_id"],),
        ).fetchall()
        for ordinal, p in enumerate(pickers):
            conn.execute(
                "INSERT OR IGNORE INTO club_meeting_hosts(meeting_id, member_id, ordinal) "
                "VALUES (?, ?, ?)",
                (r["meeting_id"], p["member_id"], ordinal),
            )

    # 4. One-time: retire club_awards. Migrate each award's book(s) into a "Books of the Year"
    #    CLUB list (note = year), then drop the three award tables. club_lists is created by
    #    CLUB_SCHEMA above; guarded on club_awards still existing → idempotent.
    if conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='club_awards'"
    ).fetchone():
        awards = conn.execute(
            "SELECT a.year, ab.book_id FROM club_awards a "
            "JOIN club_award_books ab ON ab.award_id = a.id ORDER BY a.year, ab.book_id"
        ).fetchall()
        if awards and not conn.execute(
            "SELECT 1 FROM club_lists WHERE slug = 'books-of-the-year'"
        ).fetchone():
            lid = _next_id(conn, "club_lists")
            conn.execute(
                "INSERT INTO club_lists (id, slug, name, scope, owner_id, description, created_at) "
                "VALUES (?, 'books-of-the-year', 'Books of the Year', 'club', NULL, ?, ?)",
                (lid, "The club's Book of the Year picks.",
                 datetime.datetime.now(datetime.timezone.utc).isoformat()),
            )
            for ordinal, a in enumerate(awards):
                conn.execute(
                    "INSERT OR IGNORE INTO club_list_books (list_id, book_id, ordinal, note) "
                    "VALUES (?, ?, ?, ?)",
                    (lid, a["book_id"], ordinal, str(a["year"]) if a["year"] else None),
                )
        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.executescript(
                "DROP TABLE IF EXISTS club_award_voters;\n"
                "DROP TABLE IF EXISTS club_award_books;\n"
                "DROP TABLE IF EXISTS club_awards;\n"
            )
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
        dangling = conn.execute("PRAGMA foreign_key_check").fetchall()
        if dangling:
            raise RuntimeError(f"awards->lists migration left dangling refs: {[tuple(r) for r in dangling]}")


def ensure_schema() -> None:
    """Create the club-record tables idempotently. Safe to call repeatedly."""
    with db.connect() as conn:
        conn.executescript(CLUB_SCHEMA)
        _migrate_club(conn)


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
    (ordered, de-duplicated by PK), subjects, and external enrichment — the shape
    the corpus generator needs. One pass, no N+1.

    The enrichment sidecar (club_book_enrichment) is LEFT JOINed: curated core
    columns win for the dual-source fields (synopsis/publication_year/page_count/
    isbn13/subjects_json) via COALESCE; net-new fields (ratings/editions/links/…)
    come straight from the sidecar."""
    books = _rows(
        conn,
        "SELECT b.id, b.slug, b.title, b.subtitle, b.topic, b.fiction, "
        "COALESCE(b.ol_key, e.ol_key)                     AS ol_key, "
        "COALESCE(b.publication_year, e.publication_year) AS publication_year, "
        "COALESCE(b.page_count, e.page_count)             AS page_count, "
        "COALESCE(b.isbn13, e.isbn13)                     AS isbn13, "
        "COALESCE(b.synopsis, e.synopsis)                 AS synopsis, "
        "COALESCE(b.subjects_json, e.subjects_json)       AS subjects_json, "
        "e.ol_cover_id, e.edition_count, e.languages_json, e.ratings_average, "
        "e.ratings_count, e.wikidata_id, e.wikipedia_url, e.goodreads_id, e.series, "
        "e.awards_json, e.enriched_at "
        "FROM club_books b LEFT JOIN club_book_enrichment e ON e.book_id = b.id "
        "ORDER BY b.id",
    )
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
        b["languages"] = json.loads(b["languages_json"]) if b["languages_json"] else []
        b["awards"] = json.loads(b["awards_json"]) if b["awards_json"] else []
    return books


def all_meetings(conn: sqlite3.Connection) -> list[dict]:
    meetings = _rows(conn, "SELECT * FROM club_meetings ORDER BY id")
    books_by_meeting: dict[int, list[str]] = {}
    for r in conn.execute(
        "SELECT mb.meeting_id, b.slug FROM club_meeting_books mb "
        "JOIN club_books b ON b.id = mb.book_id ORDER BY mb.meeting_id, mb.ordinal"
    ):
        books_by_meeting.setdefault(r["meeting_id"], []).append(r["slug"])
    hosts_by_meeting: dict[int, list[str]] = {}
    for r in conn.execute(
        "SELECT mh.meeting_id, m.slug FROM club_meeting_hosts mh "
        "JOIN club_members m ON m.id = mh.member_id ORDER BY mh.meeting_id, mh.ordinal"
    ):
        hosts_by_meeting.setdefault(r["meeting_id"], []).append(r["slug"])
    for m in meetings:
        m["book_slugs"] = books_by_meeting.get(m["id"], [])
        m["host_slugs"] = hosts_by_meeting.get(m["id"], [])
        m["type"] = json.loads(m["type_json"]) if m["type_json"] else []
    return meetings


def all_members(conn: sqlite3.Connection) -> list[dict]:
    return _rows(conn, "SELECT * FROM club_members ORDER BY id")


def create_member(conn: sqlite3.Connection, name: str) -> dict:
    """Add a new current member with a globally-unique slug. Returns {id, slug}."""
    name = (name or "").strip()
    if not name:
        raise ValueError("a member needs a name")
    root = slugify(name) or "member"
    slug, n = root, 2
    while conn.execute("SELECT 1 FROM club_members WHERE slug = ?", (slug,)).fetchone():
        slug, n = f"{root}-{n}", n + 1
    mid = _next_id(conn, "club_members")
    conn.execute("INSERT INTO club_members(id, slug, name, is_current) VALUES (?, ?, ?, 1)",
                 (mid, slug, name))
    return {"id": mid, "slug": slug}


def set_member_current(conn: sqlite3.Connection, member_slug: str, *, is_current: bool) -> bool:
    """Mark a member current (active) or former. Returns True if a row changed."""
    cur = conn.execute("UPDATE club_members SET is_current = ? WHERE slug = ?",
                       (1 if is_current else 0, member_slug))
    return cur.rowcount > 0


def rename_member(conn: sqlite3.Connection, member_slug: str, name: str) -> bool:
    """Change a member's display name, keeping the slug stable (slugs are identity in
    the corpus, so a rename must not move files). Returns True if a row changed."""
    name = (name or "").strip()
    if not name:
        raise ValueError("a member needs a name")
    cur = conn.execute("UPDATE club_members SET name = ? WHERE slug = ?", (name, member_slug))
    return cur.rowcount > 0


def all_authors(conn: sqlite3.Connection) -> list[dict]:
    """Every author with external enrichment LEFT JOINed in. Curated `bio` wins
    via COALESCE; net-new fields (dates/nationality/links/notable works) come from
    the sidecar. `notable_works` is decoded from its JSON column."""
    authors = _rows(
        conn,
        "SELECT a.id, a.slug, a.name, "
        "COALESCE(a.bio, e.bio) AS bio, "
        "e.birth_year, e.death_year, e.nationality, e.ol_author_key, "
        "e.wikidata_id, e.wikipedia_url, e.website, e.notable_works_json, "
        "e.photo_credit, e.enriched_at "
        "FROM club_authors a LEFT JOIN club_author_enrichment e ON e.author_id = a.id "
        "ORDER BY a.id",
    )
    for a in authors:
        a["notable_works"] = (
            json.loads(a["notable_works_json"]) if a["notable_works_json"] else []
        )
    return authors


def all_reviews(conn: sqlite3.Connection) -> list[dict]:
    """Reviews joined to book + member slugs, for corpus markdown generation."""
    return _rows(
        conn,
        "SELECT r.*, b.slug AS book_slug, m.slug AS member_slug "
        "FROM club_reviews r JOIN club_books b ON b.id = r.book_id "
        "JOIN club_members m ON m.id = r.member_id ORDER BY r.created_at, r.id",
    )


def all_lists(conn: sqlite3.Connection) -> list[dict]:
    """Every book list with owner slug + ordered entries (book slug + optional note), for corpus gen."""
    lists = _rows(
        conn,
        "SELECT l.*, m.slug AS owner_slug FROM club_lists l "
        "LEFT JOIN club_members m ON m.id = l.owner_id ORDER BY l.id",
    )
    entries_by_list: dict[int, list[dict]] = {}
    for r in conn.execute(
        "SELECT lb.list_id, b.slug AS book_slug, lb.note FROM club_list_books lb "
        "JOIN club_books b ON b.id = lb.book_id ORDER BY lb.list_id, lb.ordinal, b.slug"
    ):
        entries_by_list.setdefault(r["list_id"], []).append(
            {"book_slug": r["book_slug"], "note": r["note"]})
    for lst in lists:
        lst["entries"] = entries_by_list.get(lst["id"], [])
    return lists


def list_by_slug(conn: sqlite3.Connection, slug: str) -> dict | None:
    """One list row with owner slug projected, or None — for authorization + single-file regen."""
    row = conn.execute(
        "SELECT l.*, m.slug AS owner_slug FROM club_lists l "
        "LEFT JOIN club_members m ON m.id = l.owner_id WHERE l.slug = ?", (slug,),
    ).fetchone()
    return dict(row) if row else None


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
    # Dedupe while preserving order: some Open Library records list the same author twice
    # (e.g. Blindsight → ["Peter Watts", "Peter Watts"]), and distinct spellings can resolve
    # to one author_id — either would trip the (book_id, author_id) UNIQUE constraint below.
    author_ids: list[int] = []
    for a in (meta.get("authors") or []):
        if not a:
            continue
        aid = _author_id(conn, a)
        if aid not in author_ids:
            author_ids.append(aid)
    conn.execute("DELETE FROM club_book_authors WHERE book_id = ?", (book_id,))
    conn.executemany(
        "INSERT INTO club_book_authors(book_id, author_id, ordinal) VALUES (?, ?, ?)",
        [(book_id, aid, i) for i, aid in enumerate(author_ids)],
    )
    return {"id": book_id, "slug": slug, "existed": bool(existing), "author_ids": author_ids}


# ── Enrichment writers — the loop's ONLY DB write path (sidecars only) ────────
# Whitelisted columns; the loop passes a dict of {column: value} and we upsert by
# the 1:1 key. Core club_books/club_authors are never touched here.
_BOOK_ENRICH_COLS = (
    "ol_cover_id", "edition_count", "languages_json", "ratings_average",
    "ratings_count", "wikidata_id", "wikipedia_url", "goodreads_id", "series",
    "awards_json", "ol_key", "synopsis", "publication_year", "page_count",
    "isbn13", "subjects_json", "enriched_at", "enrichment_json",
)
_AUTHOR_ENRICH_COLS = (
    "bio", "birth_year", "death_year", "nationality", "ol_author_key",
    "wikidata_id", "wikipedia_url", "website", "notable_works_json",
    "photo_credit", "enriched_at", "enrichment_json",
)


def _upsert_enrichment(conn: sqlite3.Connection, table: str, key_col: str,
                       key_val: int, allowed: tuple[str, ...], fields: dict) -> None:
    cols = [c for c in allowed if c in fields]
    if not cols:
        return
    placeholders = ", ".join(["?"] * (len(cols) + 1))
    col_list = ", ".join([key_col, *cols])
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols)
    conn.execute(
        f"INSERT INTO {table}({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT({key_col}) DO UPDATE SET {updates}",
        (key_val, *(fields[c] for c in cols)),
    )


def upsert_book_enrichment(conn: sqlite3.Connection, book_id: int, fields: dict) -> None:
    """Upsert external enrichment for a book into club_book_enrichment (sidecar only)."""
    _upsert_enrichment(conn, "club_book_enrichment", "book_id", book_id,
                       _BOOK_ENRICH_COLS, fields)


def upsert_author_enrichment(conn: sqlite3.Connection, author_id: int, fields: dict) -> None:
    """Upsert external enrichment for an author into club_author_enrichment (sidecar only)."""
    _upsert_enrichment(conn, "club_author_enrichment", "author_id", author_id,
                       _AUTHOR_ENRICH_COLS, fields)


def book_enrichment(conn: sqlite3.Connection, book_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM club_book_enrichment WHERE book_id = ?", (book_id,)
    ).fetchone()
    return dict(row) if row else None


def author_enrichment(conn: sqlite3.Connection, author_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM club_author_enrichment WHERE author_id = ?", (author_id,)
    ).fetchone()
    return dict(row) if row else None


def upsert_review(conn: sqlite3.Connection, *, book_id: int, member_id: int,
                  rating: int | None = None, dnf: bool = False,
                  discussion_quality: int | None = None, would_recommend: bool = False,
                  favorite_quote: str | None = None, body: str | None = None,
                  created_at: str | None = None) -> dict:
    """Insert or update a review under FKs, keyed by (book_id, member_id). Preserves the existing
    row's id / created_at on update (mirrors the old markdown's id/createdAt preservation).
    Returns {id, created_at, existed}."""
    existing = conn.execute(
        "SELECT id, created_at FROM club_reviews WHERE book_id = ? AND member_id = ?",
        (book_id, member_id),
    ).fetchone()
    if existing:
        rid = existing["id"]
        created_at = existing["created_at"] or created_at
    else:
        rid = _next_id(conn, "club_reviews")
        created_at = created_at or datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO club_reviews(id, book_id, member_id, rating, dnf, "
        "discussion_quality, would_recommend, favorite_quote, body, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET rating=excluded.rating, "
        "dnf=excluded.dnf, discussion_quality=excluded.discussion_quality, "
        "would_recommend=excluded.would_recommend, favorite_quote=excluded.favorite_quote, "
        "body=excluded.body",
        (rid, book_id, member_id, rating, 1 if dnf else 0, discussion_quality,
         1 if would_recommend else 0, favorite_quote, body, created_at),
    )
    return {"id": rid, "created_at": created_at, "existed": bool(existing)}


def set_rating(conn: sqlite3.Connection, book_id: int, member_id: int, *,
               rating: int | None, dnf: bool = False) -> int:
    """Set just a member's rating/DNF for a book, PRESERVING any existing review body and other
    fields (the bulk ratings grid uses this; `upsert_review` would replace the whole row). Returns
    the review id."""
    existing = conn.execute(
        "SELECT id FROM club_reviews WHERE book_id = ? AND member_id = ?", (book_id, member_id),
    ).fetchone()
    if existing:
        conn.execute("UPDATE club_reviews SET rating = ?, dnf = ? WHERE id = ?",
                     (rating, 1 if dnf else 0, existing["id"]))
        return existing["id"]
    rid = _next_id(conn, "club_reviews")
    conn.execute(
        "INSERT INTO club_reviews(id, book_id, member_id, rating, dnf, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (rid, book_id, member_id, rating, 1 if dnf else 0, datetime.now(timezone.utc).isoformat()),
    )
    return rid


def _unique_list_slug(conn: sqlite3.Connection, base: str) -> str:
    """A globally-unique club_lists slug from `base`, with a -2/-3… suffix on collision."""
    root = slugify(base) or "list"
    slug, n = root, 2
    while conn.execute("SELECT 1 FROM club_lists WHERE slug = ?", (slug,)).fetchone():
        slug, n = f"{root}-{n}", n + 1
    return slug


def create_list(conn: sqlite3.Connection, *, name: str, scope: str,
                owner_id: int | None, description: str | None = None) -> dict:
    """Create a list. Member-list slugs are owner-prefixed to avoid cross-member collisions; club
    lists slug from the name. Returns {id, slug}."""
    base = name
    if scope == "member" and owner_id is not None:
        owner = conn.execute("SELECT slug FROM club_members WHERE id = ?", (owner_id,)).fetchone()
        base = f"{owner['slug']}-{name}" if owner else name
    slug = _unique_list_slug(conn, base)
    lid = _next_id(conn, "club_lists")
    conn.execute(
        "INSERT INTO club_lists (id, slug, name, scope, owner_id, description, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (lid, slug, name, scope, owner_id, description, datetime.now(timezone.utc).isoformat()),
    )
    return {"id": lid, "slug": slug}


def update_list(conn: sqlite3.Connection, list_id: int, *, name: str | None = None,
                description: str | None = None) -> None:
    """Update a list's name/description. The slug (URL/filename identity) is intentionally stable."""
    if name is not None:
        conn.execute("UPDATE club_lists SET name = ? WHERE id = ?", (name, list_id))
    if description is not None:
        conn.execute("UPDATE club_lists SET description = ? WHERE id = ?", (description, list_id))


def delete_list(conn: sqlite3.Connection, list_id: int) -> None:
    conn.execute("DELETE FROM club_lists WHERE id = ?", (list_id,))  # CASCADE clears club_list_books


def set_list_book(conn: sqlite3.Connection, list_id: int, book_id: int,
                  note: str | None = None) -> bool:
    """Add a book to a list (appended at the end) or update its note if already present.
    Returns True if newly added, False if it was an in-place note update."""
    if conn.execute("SELECT 1 FROM club_list_books WHERE list_id = ? AND book_id = ?",
                    (list_id, book_id)).fetchone():
        conn.execute("UPDATE club_list_books SET note = ? WHERE list_id = ? AND book_id = ?",
                     (note, list_id, book_id))
        return False
    n = conn.execute("SELECT COUNT(*) AS c FROM club_list_books WHERE list_id = ?",
                     (list_id,)).fetchone()["c"]
    conn.execute("INSERT INTO club_list_books (list_id, book_id, ordinal, note) VALUES (?,?,?,?)",
                 (list_id, book_id, n, note))
    return True


def remove_list_book(conn: sqlite3.Connection, list_id: int, book_id: int) -> bool:
    cur = conn.execute("DELETE FROM club_list_books WHERE list_id = ? AND book_id = ?",
                       (list_id, book_id))
    return cur.rowcount > 0


def move_list_book(conn: sqlite3.Connection, list_id: int, book_id: int, *, up: bool) -> bool:
    """Swap a book one step up/down in a list's order (preserving notes). Returns True if it moved."""
    rows = conn.execute(
        "SELECT book_id, ordinal FROM club_list_books WHERE list_id = ? ORDER BY ordinal",
        (list_id,)).fetchall()
    idx = next((i for i, r in enumerate(rows) if r["book_id"] == book_id), None)
    if idx is None:
        return False
    swap = idx - 1 if up else idx + 1
    if swap < 0 or swap >= len(rows):
        return False
    a, b = rows[idx], rows[swap]
    conn.execute("UPDATE club_list_books SET ordinal = ? WHERE list_id = ? AND book_id = ?",
                 (b["ordinal"], list_id, a["book_id"]))
    conn.execute("UPDATE club_list_books SET ordinal = ? WHERE list_id = ? AND book_id = ?",
                 (a["ordinal"], list_id, b["book_id"]))
    return True


def reorder_list_books(conn: sqlite3.Connection, list_id: int,
                       ordered_book_ids: list[int]) -> bool:
    """Set list ordinals to match `ordered_book_ids` (0..n). Book ids not in the list are
    ignored; any list books absent from the order keep their prior relative order at the end
    — so a partial/stale order from the client can't drop rows. Returns True."""
    existing = [r["book_id"] for r in conn.execute(
        "SELECT book_id FROM club_list_books WHERE list_id = ? ORDER BY ordinal", (list_id,))]
    existing_set = set(existing)
    seq: list[int] = []
    for bid in ordered_book_ids:
        if bid in existing_set and bid not in seq:
            seq.append(bid)
    for bid in existing:                 # append any the client didn't mention
        if bid not in seq:
            seq.append(bid)
    for ordinal, bid in enumerate(seq):
        conn.execute("UPDATE club_list_books SET ordinal = ? WHERE list_id = ? AND book_id = ?",
                     (ordinal, list_id, bid))
    return True


def set_book_picker(conn: sqlite3.Connection, book_id: int, member_id: int) -> None:
    conn.execute("DELETE FROM club_book_pickers WHERE book_id = ?", (book_id,))
    conn.execute(
        "INSERT INTO club_book_pickers(book_id, member_id, ordinal) VALUES (?, ?, 0)",
        (book_id, member_id),
    )


def set_book_pickers(conn: sqlite3.Connection, book_id: int, member_ids: list[int]) -> None:
    """Replace a book's picker(s), ordered. A book can have multiple pickers (e.g. a long book split
    between two members). Pass [] for no picker. Mirrors set_meeting_hosts."""
    conn.execute("DELETE FROM club_book_pickers WHERE book_id = ?", (book_id,))
    conn.executemany(
        "INSERT INTO club_book_pickers(book_id, member_id, ordinal) VALUES (?, ?, ?)",
        [(book_id, mid, i) for i, mid in enumerate(member_ids)],
    )


def book_picker_slugs(conn: sqlite3.Connection, book_id: int) -> list[str]:
    return [r["slug"] for r in conn.execute(
        "SELECT m.slug FROM club_book_pickers bp JOIN club_members m ON m.id = bp.member_id "
        "WHERE bp.book_id = ? ORDER BY bp.ordinal", (book_id,))]


def set_meeting_hosts(conn: sqlite3.Connection, meeting_id: int, member_ids: list[int]) -> None:
    """Replace a meeting's host(s) with the given members (ordered). Mirrors set_book_picker for the
    M:N club_meeting_hosts table."""
    conn.execute("DELETE FROM club_meeting_hosts WHERE meeting_id = ?", (meeting_id,))
    conn.executemany(
        "INSERT INTO club_meeting_hosts(meeting_id, member_id, ordinal) VALUES (?, ?, ?)",
        [(meeting_id, mid, i) for i, mid in enumerate(member_ids)],
    )


def update_meeting(conn: sqlite3.Connection, meeting_id: int, *, date: str | None = None,
                   start_time: str | None = None, location: str | None = None,
                   notes: str | None = None, types: list[str] | None = None,
                   placeholder: bool | None = None) -> None:
    """Edit an existing meeting's scalar fields. Only provided (non-None) fields are changed; pass
    placeholder=False to mark a tentative meeting as held."""
    sets, vals = [], []
    for key, val in (("date", date), ("start_time", start_time), ("location", location),
                     ("notes", notes)):
        if val is not None:
            sets.append(f"{key} = ?")
            vals.append(val)
    if types is not None:
        sets.append("type_json = ?")
        vals.append(json.dumps(types))
    if placeholder is not None:
        sets.append("placeholder = ?")
        vals.append(1 if placeholder else 0)
    if not sets:
        return
    vals.append(meeting_id)
    conn.execute(f"UPDATE club_meetings SET {', '.join(sets)} WHERE id = ?", vals)


# Meeting type tags (club_meetings.type_json is a JSON array — a meeting can be more than one).
MEETING_TYPES = ["Book", "Social", "Picking", "Planning", "Holiday"]

# The 11 single-select Topic choices (see CLAUDE.md). The admin book editor validates against these.
TOPICS = [
    "Brain & Psychology", "Current Events & People", "Essays & Literature",
    "Health & Medicine", "History & Economics", "Philosophy & Religion",
    "Politics & Social Sciences", "Science and Math", "Science Fiction & Fiction",
    "Technology", "Travel & Memoir",
]


def create_meeting(conn: sqlite3.Connection, *, date_iso: str, book_id: int | None = None,
                   types: list[str] | None = None, placeholder: bool = True) -> int:
    """Create a meeting. `book_id` is optional — a bookless social/picking meeting has none; use
    set_meeting_books for two or more."""
    mid = _next_id(conn, "club_meetings")
    conn.execute(
        "INSERT INTO club_meetings(id, date, type_json, location, notes, placeholder) "
        "VALUES (?, ?, ?, NULL, NULL, ?)",
        (mid, date_iso, json.dumps(types or ["Book"]), 1 if placeholder else 0),
    )
    if book_id is not None:
        conn.execute(
            "INSERT INTO club_meeting_books(meeting_id, book_id, ordinal) VALUES (?, ?, 0)",
            (mid, book_id),
        )
    return mid


def set_meeting_books(conn: sqlite3.Connection, meeting_id: int, book_ids: list[int]) -> None:
    """Replace a meeting's attached books (ordered). Pass [] for a bookless meeting, or 2+ for a
    multi-book meeting. Mirrors set_meeting_hosts."""
    conn.execute("DELETE FROM club_meeting_books WHERE meeting_id = ?", (meeting_id,))
    conn.executemany(
        "INSERT INTO club_meeting_books(meeting_id, book_id, ordinal) VALUES (?, ?, ?)",
        [(meeting_id, bid, i) for i, bid in enumerate(book_ids)],
    )


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


def start_time_for_meeting(meeting_id: int | None) -> str | None:
    """A meeting's LOCAL start time 'HH:MM' (America/Chicago), or None if TBD/unknown."""
    if meeting_id is None:
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT start_time FROM club_meetings WHERE id = ?", (meeting_id,)).fetchone()
    return row["start_time"] if row else None


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


def hosts_for_meeting(meeting_id: int | None) -> list[dict]:
    """Who hosted a meeting (the meeting-level host; distinct from a book's picker).
    Returns [{slug, name}] in host order."""
    if meeting_id is None:
        return []
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT m.slug, m.name FROM club_meeting_hosts mh "
            "JOIN club_members m ON m.id = mh.member_id "
            "WHERE mh.meeting_id = ? ORDER BY mh.ordinal",
            (meeting_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# Ensure the club schema + migrations at import (symmetric with db._ensure_schema), so a
# bot restart always lands the full schema (e.g. the local meeting-date normalization).
ensure_schema()


if __name__ == "__main__":
    print(json.dumps(counts(), indent=2))
