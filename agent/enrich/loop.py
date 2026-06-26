"""The enrichment loop — resolve identifiers, fetch all sources, merge gap-first,
and upsert the club_*_enrichment sidecars (the loop's ONLY DB write path).

``enrich_book`` / ``enrich_author`` enrich a single entity; ``run`` drives the
batch. The CLI wrapper lives in ``agent/enrich/__main__.py``::

    python -m agent.enrich [--books] [--authors] [--force] [--limit N] [--slug X]

Gap-filling + idempotent: rows already stamped with ``enriched_at`` are skipped
unless ``--force``. Curated core fields are never overwritten — dual-source values
(synopsis/bio/year/pages/isbn/subjects) are only mirrored into the sidecar when the
*effective* (already-COALESCEd) value is empty. Images are fetched to disk
(covers / author portraits) and stay filesystem assets, not DB rows.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from agent import clubdb, db
from agent.enrich import openlibrary as ol
from agent.enrich import wikidata as wd
from agent.enrich import wikipedia as wp
from corpus import images
from corpus.paths import AUTHORS_IMG_DIR, COVERS_DIR

COVER_WIDTHS = [240, 480, 960]
AUTHOR_PHOTO_WIDTHS = [240, 480]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _put(fields: dict, key: str, value) -> None:
    """Set a sidecar field only when there's a real value (skip None/empty)."""
    if value not in (None, "", [], {}):
        fields[key] = value


def _put_json(fields: dict, key: str, value) -> None:
    if value:
        fields[key] = json.dumps(value, ensure_ascii=False)


def _has_image(directory, slug: str) -> bool:
    return directory.exists() and any(directory.glob(f"{slug}-*.jpg"))


def _fetch_image(url: str | None, slug: str, out_dir, widths) -> list[int] | None:
    if not url:
        return None
    try:
        return images.process_image(url, slug, out_dir, widths)
    except Exception as e:  # noqa: BLE001 - image fetch is non-fatal
        print(f"      image fetch failed for {slug}: {e}", file=sys.stderr)
        return None


# ── Book ─────────────────────────────────────────────────────────────────────
def enrich_book(conn, book: dict, *, force: bool = False, fetch_images: bool = True) -> dict:
    title, authors = book["title"], book["author_names"]
    olf = ol.book_facts(title, authors, book.get("ol_key"), book.get("isbn13"))

    ent = wd.resolve_book(title, authors)
    wdf = wd.book_facts(ent) if ent else {}

    fields: dict = {}
    # Net-new external fields (no core column).
    _put(fields, "ol_cover_id", olf.get("ol_cover_id"))
    _put(fields, "edition_count", olf.get("edition_count"))
    _put_json(fields, "languages_json", olf.get("languages"))
    _put(fields, "ratings_average", olf.get("ratings_average"))
    _put(fields, "ratings_count", olf.get("ratings_count"))
    _put(fields, "wikidata_id", wdf.get("wikidata_id"))
    _put(fields, "wikipedia_url", wdf.get("wikipedia_url"))
    _put(fields, "goodreads_id", wdf.get("goodreads_id"))
    _put(fields, "series", wdf.get("series"))
    _put_json(fields, "awards_json", wdf.get("awards"))
    # Dual-source mirrors — only when the effective value is empty (gap-fill).
    if not book.get("ol_key"):
        _put(fields, "ol_key", olf.get("ol_key"))
    if not book.get("synopsis"):
        _put(fields, "synopsis", olf.get("synopsis"))
    if not book.get("publication_year"):
        _put(fields, "publication_year", olf.get("publication_year"))
    if not book.get("page_count"):
        _put(fields, "page_count", olf.get("page_count"))
    if not book.get("isbn13"):
        _put(fields, "isbn13", olf.get("isbn13"))
    if not book.get("subjects"):
        _put_json(fields, "subjects_json", olf.get("subjects"))

    fields["enriched_at"] = _now()
    fields["enrichment_json"] = json.dumps(
        {"openlibrary": {k: v for k, v in olf.items() if k != "author_keys"},
         "wikidata": wdf}, ensure_ascii=False)
    clubdb.upsert_book_enrichment(conn, book["id"], fields)

    cover_id = olf.get("ol_cover_id")
    if fetch_images and cover_id and (force or not _has_image(COVERS_DIR, book["slug"])):
        _fetch_image(ol.cover_url(cover_id), book["slug"], COVERS_DIR, COVER_WIDTHS)
    return fields


# ── Author ───────────────────────────────────────────────────────────────────
def _discover_ol_author_key(conn, author_id: int, name: str) -> str | None:
    """Resolve the OL author key from one of the author's read books (authoritative
    — it's literally the author of a book we've read). Prefer books that already
    carry an OL Work key; fall back to one search for an unkeyed book."""
    rows = conn.execute(
        "SELECT b.title, COALESCE(b.ol_key, e.ol_key) AS ol_key "
        "FROM club_book_authors ba JOIN club_books b ON b.id = ba.book_id "
        "LEFT JOIN club_book_enrichment e ON e.book_id = b.id "
        "WHERE ba.author_id = ? ORDER BY (b.ol_key IS NULL)", (author_id,),
    ).fetchall()
    for r in rows:
        ol_key = r["ol_key"]
        if not ol_key:
            doc = ol.search_best_match(r["title"], [name])
            ol_key = (doc or {}).get("key")
        key = ol.resolve_author_key(ol.work(ol_key), name)
        if key:
            return key
    return None


def enrich_author(conn, author: dict, *, force: bool = False, fetch_images: bool = True) -> dict:
    name, slug = author["name"], author["slug"]
    ol_author_key = author.get("ol_author_key") or _discover_ol_author_key(
        conn, author["id"], name)
    olf = ol.author_facts(ol.author(ol_author_key))

    ent = wd.resolve_author(name, ol_wikidata=olf.get("wikidata_id"),
                            birth_year_hint=olf.get("birth_year"))
    wdf = wd.author_facts(ent) if ent else {}
    wpf = wp.summary(slug, name, wikipedia_title=wdf.get("wikipedia_title")) or {}

    fields: dict = {}
    if not author.get("bio"):  # gap-fill curated bio: OL bio, else Wikipedia extract
        _put(fields, "bio", olf.get("bio") or wpf.get("extract"))
    _put(fields, "birth_year", olf.get("birth_year") or wdf.get("birth_year"))
    _put(fields, "death_year", olf.get("death_year") or wdf.get("death_year"))
    _put(fields, "nationality", wdf.get("nationality"))
    _put(fields, "ol_author_key", ol_author_key)
    _put(fields, "wikidata_id", wdf.get("wikidata_id"))
    _put(fields, "wikipedia_url", wdf.get("wikipedia_url") or wpf.get("wikipedia_url"))
    _put(fields, "website", olf.get("website") or wdf.get("website"))
    _put_json(fields, "notable_works_json", wdf.get("notable_works"))

    # Portrait: OL author photo → Wikidata Commons image → Wikipedia thumbnail.
    photo_url = credit = None
    if olf.get("ol_photo_id"):
        photo_url, credit = ol.author_photo_url(olf["ol_photo_id"]), "Open Library"
    elif wdf.get("image_filename"):
        photo_url, credit = wd.commons_image_url(wdf["image_filename"]), "Wikimedia Commons"
    elif wpf.get("thumbnail_url"):
        photo_url, credit = wpf["thumbnail_url"], "Wikipedia"
    if fetch_images and photo_url and (force or not _has_image(AUTHORS_IMG_DIR, slug)):
        if _fetch_image(photo_url, slug, AUTHORS_IMG_DIR, AUTHOR_PHOTO_WIDTHS):
            _put(fields, "photo_credit", credit)

    fields["enriched_at"] = _now()
    fields["enrichment_json"] = json.dumps(
        {"openlibrary": olf, "wikidata": wdf,
         "wikipedia": {k: v for k, v in wpf.items() if k != "extract"}},
        ensure_ascii=False)
    clubdb.upsert_author_enrichment(conn, author["id"], fields)
    return fields


# ── Runner ───────────────────────────────────────────────────────────────────
def _already_enriched(conn, table: str, key_col: str) -> set[int]:
    return {r[key_col] for r in conn.execute(
        f"SELECT {key_col} FROM {table} WHERE enriched_at IS NOT NULL")}


def _select(entities: list[dict], done: set[int], *, id_key: str,
            force: bool, slug: str | None, limit: int | None) -> list[dict]:
    todo = entities if force else [e for e in entities if e[id_key] not in done]
    if slug:
        todo = [e for e in todo if e["slug"] == slug]
    if limit:
        todo = todo[:limit]
    return todo


def run(*, do_books: bool, do_authors: bool, force: bool = False,
        limit: int | None = None, slug: str | None = None,
        fetch_images: bool = True) -> dict:
    counts = {"books": 0, "authors": 0}
    with db.connect() as conn:
        if do_books:
            done = _already_enriched(conn, "club_book_enrichment", "book_id")
            todo = _select(clubdb.all_books(conn), done, id_key="id",
                           force=force, slug=slug, limit=limit)
            print(f"books: enriching {len(todo)} (skipping {len(done)} already done)")
            for b in todo:
                f = enrich_book(conn, b, force=force, fetch_images=fetch_images)
                conn.commit()
                counts["books"] += 1
                print(f"  ✓ {b['slug']}  "
                      f"[{', '.join(k for k in f if k not in ('enriched_at', 'enrichment_json'))}]")
        if do_authors:
            done = _already_enriched(conn, "club_author_enrichment", "author_id")
            todo = _select(clubdb.all_authors(conn), done, id_key="id",
                           force=force, slug=slug, limit=limit)
            print(f"authors: enriching {len(todo)} (skipping {len(done)} already done)")
            for a in todo:
                f = enrich_author(conn, a, force=force, fetch_images=fetch_images)
                conn.commit()
                counts["authors"] += 1
                print(f"  ✓ {a['slug']}  "
                      f"[{', '.join(k for k in f if k not in ('enriched_at', 'enrichment_json'))}]")
    return counts


