"""Best-effort Open Library metadata lookup for the web app admin add-book flow.

Open Library data is uneven, so this returns a single best-guess candidate; the admin
reviews/edits the written file. Looks up by ISBN (precise) or title (search).
"""

from __future__ import annotations

import re

import requests

OL = "https://openlibrary.org"
LOOKUP_TIMEOUT = 8  # admin form: fail into manual entry instead of making the page look hung


def normalize_isbn13(value: str | None) -> str | None:
    """Return a punctuation-free ISBN-13, or None for other ISBN forms."""
    digits = re.sub(r"[^0-9X]", "", str(value or "").upper())
    return digits if len(digits) == 13 else None


def _isbn13(values) -> str | None:
    for value in values or []:
        isbn13 = normalize_isbn13(value)
        if isbn13:
            return isbn13
    return None


def _by_isbn(isbn: str) -> dict | None:
    query_isbn = normalize_isbn13(isbn) or isbn.strip()
    try:
        r = requests.get(f"{OL}/isbn/{query_isbn}.json", timeout=LOOKUP_TIMEOUT)
        if not r.ok:
            return None
        ed = r.json()
    except Exception:
        return None
    work_key = (ed.get("works") or [{}])[0].get("key")
    year = None
    m = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", str(ed.get("publish_date") or ""))
    if m:
        year = int(m.group(1))
    return {
        "title": ed.get("title"),
        "authors": [],  # edition authors are keys; the enrichment sweep resolves them later
        "publicationYear": year,
        "pageCount": ed.get("number_of_pages"),
        "isbn13": normalize_isbn13(isbn),
        "olKey": work_key,
        "synopsis": None,
    }


def _by_title(title: str) -> dict | None:
    try:
        r = requests.get(
            f"{OL}/search.json",
            params={
                "title": title,
                "limit": 1,
                "fields": "key,title,author_name,first_publish_year,isbn,number_of_pages_median",
            },
            timeout=LOOKUP_TIMEOUT,
        )
        docs = (r.json().get("docs") or []) if r.ok else []
    except Exception:
        return None
    if not docs:
        return None
    d = docs[0]
    return {
        "title": d.get("title"),
        "authors": d.get("author_name") or [],
        "publicationYear": d.get("first_publish_year"),
        "pageCount": d.get("number_of_pages_median"),
        "isbn13": _isbn13(d.get("isbn")),
        "olKey": d.get("key"),
        "synopsis": None,
    }


def lookup(title: str | None = None, isbn: str | None = None) -> dict | None:
    """Return a best-guess metadata dict, or None if nothing matched."""
    # An explicit ISBN identifies an edition. If that precise lookup is unavailable,
    # let the admin form preserve their title + ISBN manually; a broad title search
    # can silently select a different edition, language, or ISBN.
    if isbn:
        return _by_isbn(isbn)
    return _by_title(title) if title else None
