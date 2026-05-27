"""Builds the corpus context Oliver gets as background knowledge.

Reads the canonical book list from the corpus package (the same JSON the
website renders from) and renders a compact text digest. Kept small on purpose
so it fits comfortably in a cached system prompt.
"""

from __future__ import annotations

import json

from corpus.airtable import DATA_DIR

BOOKS_DIR = DATA_DIR / "books"


def _load_books() -> list[dict]:
    if not BOOKS_DIR.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(BOOKS_DIR.glob("*.json"))]


def book_count() -> int:
    return len(_load_books())


def _book_line(b: dict) -> str:
    authors = ", ".join(b.get("authors") or []) or "unknown author"
    year = b.get("publicationYear")
    pub = f", {year}" if year else ""
    topic = b.get("topic") or "Uncategorized"
    kind = "fiction" if b.get("fiction") else "non-fiction"
    if b.get("placeholder"):
        read = "upcoming"
    elif b.get("year"):
        read = f"read {b['year']}"
    else:
        read = "unread"
    picker = b.get("pickerName")
    by = f"; picked by {picker}" if picker else ""
    return f"- {b.get('title')} — {authors}{pub} [{topic}, {kind}] ({read}{by})"


def club_context() -> str:
    """A compact, model-facing digest of the club and its reading list."""
    books = _load_books()
    header = (
        "The R/W Book Club has met monthly since April 2003, reading about eight "
        "books a year — mostly non-fiction. Members take turns picking and hosting. "
        f"The corpus currently holds {len(books)} books:"
    )
    lines = [_book_line(b) for b in books]
    return header + "\n\n" + "\n".join(lines)
