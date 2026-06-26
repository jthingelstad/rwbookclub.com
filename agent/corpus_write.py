"""Write books + meetings — the /oliver add-book / schedule path.

Oliver manages the authoritative SQLite club record (``club_*`` tables); the Git corpus is
then regenerated from it and published via gitwrite. So a write is:

    db transaction (upsert under FKs) → regenerate the affected corpus file(s) → validate
    → fetch cover → gitwrite.commit_paths

The DB's real foreign keys enforce referential integrity at write time, so the corpus
``validate`` is now a belt-and-suspenders post-check. The transaction is the commit point;
if regeneration or git fails afterwards the DB stays ahead and self-heals on the next regen.
"""

from __future__ import annotations

import logging
import os

from corpus.paths import DATA_DIR
from corpus.validate import validate_data_dir
from agent import clubdb, corpus_gen, db, gitwrite
from agent import corpus_read as cr

log = logging.getLogger(__name__)


class WriteError(Exception):
    """User-facing problem (missing title, unknown book/member) — surfaced in Discord."""


def _validate_or_raise() -> None:
    errors = validate_data_dir(DATA_DIR)
    if errors:
        preview = "; ".join(errors[:3])
        more = f" (+{len(errors) - 3} more)" if len(errors) > 3 else ""
        raise WriteError(f"Corpus validation failed: {preview}{more}")


def _enrich_new_book(book_id: int, author_ids: list[int], out_dir) -> list:
    """Inline external enrichment for a freshly added book + its authors, so a new
    book lands rich (ratings, editions, links, author bios/portraits) instead of
    waiting for the next `python -m agent.enrich` pass. Best-effort and isolated in
    its own connection — network I/O never holds the main write transaction, and any
    failure is non-fatal (the batch loop fills the gap later). Returns author
    portrait paths to include in the git commit."""
    # Off in tests (and any offline context) so writes stay deterministic + network-free.
    if os.environ.get("OLIVER_ENRICH_ON_WRITE", "1") != "1":
        return []
    imgs: list = []
    try:
        from agent.enrich.loop import enrich_author, enrich_book
        from corpus.paths import AUTHORS_IMG_DIR
        with db.connect() as conn:
            book = next(b for b in clubdb.all_books(conn) if b["id"] == book_id)
            enrich_book(conn, book, fetch_images=False)  # cover via _fetch_cover
            conn.commit()
            for a in clubdb.all_authors(conn):
                if a["id"] in author_ids:
                    enrich_author(conn, a, fetch_images=True)
                    conn.commit()
                    imgs += sorted(AUTHORS_IMG_DIR.glob(f"{a['slug']}-*.jpg"))
            # Regenerate the affected files now that the sidecars are populated.
            corpus_gen.write_book_file(conn, book_id, out_dir)
            for aid in author_ids:
                corpus_gen.write_author_file(conn, aid, out_dir)
    except Exception:  # noqa: BLE001 - enrichment is best-effort
        log.exception("inline enrichment failed (non-fatal)")
    return imgs


def _fetch_cover(slug: str, ol_key: str | None) -> list:
    if not ol_key:
        return []
    try:
        from corpus.images import COVERS_DIR, COVER_WIDTHS, ol_cover_url, process_image
        url = ol_cover_url(ol_key)
        if not url:
            return []
        widths = process_image(url, slug, COVERS_DIR, COVER_WIDTHS)
        return [COVERS_DIR / f"{slug}-{w}.jpg" for w in widths]
    except Exception:  # noqa: BLE001 - a missing cover is non-fatal
        return []


def write_book(meta: dict) -> dict:
    title = (meta.get("title") or "").strip()
    if not title:
        raise WriteError("A book needs a title.")
    gitwrite.sync()
    clubdb.ensure_schema()
    with db.connect() as conn:                          # transaction = commit point
        res = clubdb.upsert_book(conn, meta)
        bpath = corpus_gen.write_book_file(conn, res["id"], DATA_DIR)
        apaths = [corpus_gen.write_author_file(conn, aid, DATA_DIR) for aid in res["author_ids"]]
    # Enrich the new book + its authors (separate connection; regenerates the files).
    portraits = _enrich_new_book(res["id"], res["author_ids"], DATA_DIR)
    _validate_or_raise()
    covers = _fetch_cover(res["slug"], meta.get("olKey"))
    gitwrite.commit_paths(
        [bpath, *apaths, *covers, *portraits],
        f"{'Update' if res['existed'] else 'Add'} book: {title}",
    )
    return {"slug": res["slug"], "title": title, "authors": meta.get("authors") or [],
            "hasCover": bool(covers), "updated": res["existed"]}


def schedule_meeting(book_query: str, date_iso: str, picker_query: str) -> dict:
    gitwrite.sync()
    clubdb.ensure_schema()
    book = cr.find_book(book_query)
    if not book:
        raise WriteError(f"No book matching {book_query!r} — add it first with /oliver add-book.")
    member = cr.find_member(picker_query)
    if not member:
        raise WriteError(f"No club member matching {picker_query!r}.")
    day = (date_iso or "").strip()[:10]
    if not day:
        raise WriteError("A meeting needs a date (YYYY-MM-DD).")
    iso = f"{day}T00:00:00.000Z" if len(day) == 10 else date_iso

    with db.connect() as conn:
        book_id = clubdb.book_id_for_slug(conn, book["slug"])
        member_id = clubdb.member_id_for_slug(conn, member["slug"])
        if book_id is None or member_id is None:
            raise WriteError("Book or member is not in the club database yet.")
        clubdb.set_book_picker(conn, book_id, member_id)
        meeting_id = clubdb.create_meeting(conn, date_iso=iso, book_id=book_id, placeholder=True)
        bpath = corpus_gen.write_book_file(conn, book_id, DATA_DIR)
        mpath = corpus_gen.write_meeting_file(conn, meeting_id, DATA_DIR)
    _validate_or_raise()
    gitwrite.commit_paths(
        [bpath, mpath],
        f"Schedule {book['title']} for {day} (picked by {member['name']})",
    )
    return {"book": book["title"], "date": day, "picker": member["name"]}
