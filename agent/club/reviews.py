"""Write a member's book review into the Git corpus.

The single review writer, called by the /review modal (and any future front-end).
Validates the form fields, resolves book/member rec ids, writes
reviews/<book-slug>--<member-slug>.md in the exact Phase-1 format, and commits via
gitwrite. Updating an existing review preserves its id and createdAt.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

import yaml

from corpus.paths import DATA_DIR
from corpus.validate import validate_data_dir
from agent import corpus_read as cr
from agent import gitwrite

REVIEWS_DIR = DATA_DIR / "reviews"


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


def _existing(path) -> dict:
    if not path.exists():
        return {}
    data, _ = cr.parse_frontmatter(path.read_text())
    return data or {}


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
    gitwrite.sync()

    rating_val, dnf = _parse_rating(rating)
    discussion_val = _parse_1to5(discussion)
    body = (review or "").strip()
    quote_val = (quote or "").strip() or None
    if rating_val is None and not dnf and not body:
        raise ReviewError("A review needs at least a rating, a DNF, or some text.")

    path = REVIEWS_DIR / f"{book['slug']}--{member['slug']}.md"
    previous_text = path.read_text() if path.exists() else None
    prev = _existing(path)
    front = {
        "id": prev.get("id") or f"rev_{uuid.uuid4().hex[:16]}",
        "book": book["slug"],
        "member": member["slug"],
        "rating": rating_val,
        "dnf": dnf,
        "discussionQuality": discussion_val,
        "wouldRecommend": _parse_bool(recommend),
        "favoriteQuote": quote_val,
        "createdAt": prev.get("createdAt") or datetime.now(timezone.utc).isoformat(),
    }
    fm = yaml.safe_dump(front, sort_keys=False, allow_unicode=True, default_flow_style=False)
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm}---\n\n{body}\n" if body else f"---\n{fm}---\n")

    verb = "Update" if prev else "Add"
    try:
        _validate_or_raise()
        sha = gitwrite.commit_paths([path], f"{verb} review of {book['title']} by {member['name']}")
    except Exception:
        if previous_text is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(previous_text)
        raise
    return {
        "book": book["title"],
        "member": member["name"],
        "rating": rating_val,
        "dnf": dnf,
        "updated": bool(prev),
        "committed": sha,
        "path": str(path),
    }
