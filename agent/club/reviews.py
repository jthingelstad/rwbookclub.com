"""Write a member's book review into the authoritative club record (SQLite).

The single review writer, called by the /review modal (and any future front-end).
Validates the form fields, resolves book/member ids, upserts ``club_reviews``, then
regenerates the corpus review file (``reviews/<book>--<member>.md``) from the DB.
Updating an existing review preserves its id and createdAt. The site is rebuilt +
deployed separately by the publish step (the corpus is no longer committed to git).
"""

from __future__ import annotations

import re
import uuid

from corpus.paths import DATA_DIR
from corpus.validate import validate_data_dir
from agent import clubdb, corpus_gen, db
from agent import corpus_read as cr


class ReviewError(Exception):
    """A user-facing problem (bad input, unknown book/member) — surfaced in Discord."""


def _parse_rating(value: str | None) -> tuple[int | None, bool]:
    """Returns (rating 1-5 or None, dnf)."""
    s = (value or "").strip().lower()
    if not s:
        return None, False
    if s.replace(" ", "") in {"dnf", "didnotfinish", "didn'tfinish"}:
        return None, True
    # Anchored: "5" works, "11" / "5stars" / "5/5" reject. The modal label
    # says "Rating (1–5, or DNF)" so a single digit is the contract.
    m = re.fullmatch(r"[1-5]", s)
    if m:
        return int(s), False
    raise ReviewError(f"Rating should be 1–5 or DNF (got {value!r}).")


def _parse_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"y", "yes", "true", "1", "yep", "sure"}


def _parse_1to5(value: str | None) -> int | None:
    s = (value or "").strip()
    if not s:
        return None
    if not re.fullmatch(r"[1-5]", s):
        raise ReviewError(f"Discussion quality should be 1–5 (got {value!r}).")
    return int(s)


def _validate_or_raise() -> None:
    errors = validate_data_dir(DATA_DIR)
    if errors:
        preview = "; ".join(errors[:3])
        more = f" (+{len(errors) - 3} more)" if len(errors) > 3 else ""
        raise ReviewError(f"Corpus validation failed: {preview}{more}")


def write_review(book_query: str, member_name: str, *, rating: str | None = None,
                 review: str | None = None, recommend: str | None = None,
                 discussion: str | None = None, quote: str | None = None) -> dict:
    member = cr.find_member(member_name)
    if not member:
        raise ReviewError("I can only record reviews from club members.")
    book = cr.find_book(book_query)
    if not book:
        raise ReviewError(f"I couldn't find a book matching {book_query!r}.")

    rating_val, dnf = _parse_rating(rating)
    discussion_val = _parse_1to5(discussion)
    body = (review or "").strip()
    quote_val = (quote or "").strip() or None
    if rating_val is None and not dnf and not body:
        raise ReviewError("A review needs at least a rating, a DNF, or some text.")

    # DB-backed write (the club record is authoritative); the corpus review file is then
    # regenerated from the DB. A new review mints a `rev_*` external id (stored as
    # airtable_id), preserved across edits — mirrors the old markdown id/createdAt behavior.
    with db.connect() as conn:                          # transaction = commit point
        book_id = clubdb.book_id_for_slug(conn, book["slug"])
        member_id = clubdb.member_id_for_slug(conn, member["slug"])
        if book_id is None or member_id is None:
            raise ReviewError("That book or member isn't in the club database yet.")
        res = clubdb.upsert_review(
            conn, book_id=book_id, member_id=member_id, rating=rating_val, dnf=dnf,
            discussion_quality=discussion_val, would_recommend=_parse_bool(recommend),
            favorite_quote=quote_val, body=body or None,
            airtable_id=f"rev_{uuid.uuid4().hex[:16]}",
        )
        path = corpus_gen.write_review_file(conn, res["id"], DATA_DIR)
    _validate_or_raise()
    return {
        "book": book["title"],
        "member": member["name"],
        "rating": rating_val,
        "dnf": dnf,
        "updated": res["existed"],
        "path": str(path),
    }
