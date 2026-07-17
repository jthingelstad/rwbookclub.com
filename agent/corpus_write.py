"""Write books + meetings — the add-book / schedule-meeting write path (web app admin Books/Meetings).

Oliver manages the authoritative SQLite club record (``club_*`` tables). A write is:

    db transaction (upsert under FKs) → regenerate the affected corpus file(s) → validate

The DB's real foreign keys enforce referential integrity at write time, so the corpus
``validate`` is a belt-and-suspenders post-check. The transaction is the commit point; the
corpus is private/local (gitignored), and the caller schedules a background publish that
rebuilds + deploys the site. Nothing is committed to git here.
"""

from __future__ import annotations

import logging
import os

from agent import clubdb, corpus_gen, db
from agent import corpus_read as cr
from corpus.paths import DATA_DIR
from corpus.validate import validate_data_dir

log = logging.getLogger(__name__)


class WriteError(Exception):
    """User-facing problem (missing title, unknown book/member) — surfaced in Discord."""


def _validate_or_raise() -> None:
    # Incremental projections use the same versioned contract as a full regeneration.
    corpus_gen.write_manifest(DATA_DIR)
    errors = validate_data_dir(DATA_DIR)
    if errors:
        preview = "; ".join(errors[:3])
        more = f" (+{len(errors) - 3} more)" if len(errors) > 3 else ""
        raise WriteError(f"Corpus validation failed: {preview}{more}")


def _enrich_new_book(book_id: int, author_ids: list[int], out_dir) -> None:
    """Inline external enrichment for a freshly added book + its authors, so a new book
    lands rich (cover, ratings, editions, links, author bios/portraits) instead of waiting
    for the next `python -m agent.enrich` pass. Best-effort and isolated in its own
    connection — network I/O never holds the main write transaction; failure is non-fatal
    (the batch loop fills the gap later)."""
    # Off in tests (and any offline context) so writes stay deterministic + network-free.
    if os.environ.get("OLIVER_ENRICH_ON_WRITE", "1") != "1":
        return
    try:
        from agent.enrich.loop import enrich_author, enrich_book
        with db.connect() as conn:
            book = next(b for b in clubdb.all_books(conn) if b["id"] == book_id)
            enrich_book(conn, book, fetch_images=True)  # owns the cover fetch
            conn.commit()
            for a in clubdb.all_authors(conn):
                if a["id"] in author_ids:
                    enrich_author(conn, a, fetch_images=True)
                    conn.commit()
            # Regenerate the affected files now that the sidecars are populated.
            corpus_gen.write_book_file(conn, book_id, out_dir)
            for aid in author_ids:
                corpus_gen.write_author_file(conn, aid, out_dir)
    except Exception:
        log.exception("inline enrichment failed (non-fatal)")


def write_book(meta: dict) -> dict:
    title = (meta.get("title") or "").strip()
    if not title:
        raise WriteError("A book needs a title.")
    with db.connect() as conn:                          # transaction = commit point
        res = clubdb.upsert_book(conn, meta)
        corpus_gen.write_book_file(conn, res["id"], DATA_DIR)
        for aid in res["author_ids"]:
            corpus_gen.write_author_file(conn, aid, DATA_DIR)
    # Enrich the new book + its authors (separate connection; fetches cover/portraits,
    # regenerates the files). The caller schedules a background publish to deploy.
    _enrich_new_book(res["id"], res["author_ids"], DATA_DIR)
    _validate_or_raise()
    from corpus.images import has_cover
    return {"slug": res["slug"], "title": title, "authors": meta.get("authors") or [],
            "hasCover": has_cover(res["slug"]), "updated": res["existed"]}


def schedule_meeting(book_query: str, date_iso: str, picker_query: str) -> dict:
    book = cr.find_book(book_query)
    if not book:
        raise WriteError(f"No book matching {book_query!r} — add it first in the web app (Books → Add).")
    member = cr.find_member(picker_query)
    if not member:
        raise WriteError(f"No club member matching {picker_query!r}.")
    day = (date_iso or "").strip()[:10]
    if len(day) != 10:
        raise WriteError("A meeting needs a date (YYYY-MM-DD).")

    with db.connect() as conn:
        book_id = clubdb.book_id_for_slug(conn, book["slug"])
        member_id = clubdb.member_id_for_slug(conn, member["slug"])
        if book_id is None or member_id is None:
            raise WriteError("Book or member is not in the club database yet.")
        # Store the bare LOCAL date (YYYY-MM-DD) — the club_meetings.date contract. The old
        # 'day + T00:00:00.000Z' form injected a UTC instant that broke naive date parsing
        # downstream (e.g. datetime.fromisoformat in the scheduler).
        meeting_id = clubdb.create_meeting(conn, date_iso=day, book_id=book_id)
        # The meeting's host IS the picker; the book's picker derives from this host.
        clubdb.set_meeting_hosts(conn, meeting_id, [member_id])
        corpus_gen.write_book_file(conn, book_id, DATA_DIR)
        corpus_gen.write_meeting_file(conn, meeting_id, DATA_DIR)
    # Chronicle hook: drop a meeting_scheduled event on the club timeline at the meeting's
    # date so the event log reflects future meetings, not just past ones.
    db.record_meeting_scheduled(
        meeting_id,
        actor="oliver",
        detail={"book": book["title"], "date": day, "picker": member["name"]},
        occurred_at=day,
    )
    _validate_or_raise()
    # Corpus is private/local now; the site is rebuilt + deployed by the publish step
    # (the caller schedules it). Nothing is committed to git here.
    return {"book": book["title"], "date": day, "picker": member["name"]}
