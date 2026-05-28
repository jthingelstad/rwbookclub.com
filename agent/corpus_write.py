"""Write books + meetings into the Git corpus — the /oliver add-book / schedule path.

Produces the normalized, hand-editable shapes (no rec id; slug = filename) and commits
via gitwrite. Book covers are fetched from Open Library and committed alongside the book.
"""

from __future__ import annotations

import json

from corpus.paths import DATA_DIR, slugify
from corpus.validate import validate_data_dir
from agent import corpus_read as cr
from agent import gitwrite


class WriteError(Exception):
    """User-facing problem (missing title, unknown book/member) — surfaced in Discord."""


def _validate_or_raise() -> None:
    errors = validate_data_dir(DATA_DIR)
    if errors:
        preview = "; ".join(errors[:3])
        more = f" (+{len(errors) - 3} more)" if len(errors) > 3 else ""
        raise WriteError(f"Corpus validation failed: {preview}{more}")


def _max_int(name: str, field: str) -> int:
    vals = [json.loads(p.read_text()).get(field) or 0 for p in (DATA_DIR / name).glob("*.json")]
    return max(vals) if vals else 0


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
    slug = slugify(title)
    rec = {
        "bookId": meta.get("bookId") or (_max_int("books", "bookId") + 1),
        "title": title,
        "subtitle": meta.get("subtitle"),
        "authors": meta.get("authors") or [],
        "topic": meta.get("topic"),
        "fiction": bool(meta.get("fiction")),
        "publicationYear": meta.get("publicationYear"),
        "pageCount": meta.get("pageCount"),
        "isbn13": meta.get("isbn13"),
        "olKey": meta.get("olKey"),
        "synopsis": meta.get("synopsis"),
    }
    path = DATA_DIR / "books" / f"{slug}.json"
    existed = path.exists()
    previous = path.read_text() if existed else None
    author_previous: dict = {}
    author_paths = []
    path.write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
    try:
        author_dir = DATA_DIR / "authors"
        author_dir.mkdir(parents=True, exist_ok=True)
        for author in rec["authors"]:
            if not author:
                continue
            apath = author_dir / f"{slugify(author)}.json"
            if apath.exists():
                continue
            author_previous[apath] = None
            apath.write_text(json.dumps({"name": author, "bio": None}, indent=2, ensure_ascii=False) + "\n")
            author_paths.append(apath)
        _validate_or_raise()
        covers = _fetch_cover(slug, rec["olKey"])
        gitwrite.commit_paths([path, *author_paths, *covers],
                              f"{'Update' if existed else 'Add'} book: {title}")
    except Exception:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(previous)
        for apath in author_previous:
            apath.unlink(missing_ok=True)
        raise
    return {"slug": slug, "title": title, "authors": rec["authors"],
            "hasCover": bool(covers), "updated": existed}


def schedule_meeting(book_query: str, date_iso: str, picker_query: str) -> dict:
    gitwrite.sync()
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

    # Set the picker on the book file (preserve its other fields).
    bpath = DATA_DIR / "books" / f"{book['slug']}.json"
    previous_book = bpath.read_text()
    braw = json.loads(bpath.read_text())
    braw["picker"] = [member["slug"]]
    bpath.write_text(json.dumps(braw, indent=2, ensure_ascii=False) + "\n")

    # Create the (placeholder) meeting.
    mid = _max_int("meetings", "meetingId") + 1
    mpath = DATA_DIR / "meetings" / f"{day}--{mid}.json"
    mpath.write_text(json.dumps({
        "meetingId": mid, "date": iso, "books": [book["slug"]],
        "type": ["Book"], "location": None, "notes": None, "placeholder": True,
    }, indent=2, ensure_ascii=False) + "\n")

    try:
        _validate_or_raise()
        gitwrite.commit_paths(
            [bpath, mpath],
            f"Schedule {book['title']} for {day} (picked by {member['name']})",
        )
    except Exception:
        bpath.write_text(previous_book)
        mpath.unlink(missing_ok=True)
        raise
    return {"book": book["title"], "date": day, "picker": member["name"]}
