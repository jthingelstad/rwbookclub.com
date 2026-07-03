"""Best-effort Open Library metadata lookup for the web app admin add-book flow.

Open Library data is uneven, so this returns a single best-guess candidate; the admin
reviews/edits the written file. Looks up by ISBN (precise) or title (search).
"""

from __future__ import annotations

import re

import requests

OL = "https://openlibrary.org"


def _work_meta(work_key: str | None) -> dict:
    if not work_key:
        return {}
    try:
        w = requests.get(f"{OL}{work_key}.json", timeout=20).json()
    except Exception:  # noqa: BLE001
        return {}
    desc = w.get("description")
    if isinstance(desc, dict):
        desc = desc.get("value")
    return {"synopsis": (desc or None), "title": w.get("title")}


def _isbn13(values) -> str | None:
    for v in values or []:
        digits = re.sub(r"[^0-9X]", "", str(v))
        if len(digits) == 13:
            return digits
    return None


def _by_isbn(isbn: str) -> dict | None:
    try:
        r = requests.get(f"{OL}/isbn/{isbn}.json", timeout=20)
        if not r.ok:
            return None
        ed = r.json()
    except Exception:  # noqa: BLE001
        return None
    work_key = (ed.get("works") or [{}])[0].get("key")
    meta = _work_meta(work_key)
    year = None
    m = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", str(ed.get("publish_date") or ""))
    if m:
        year = int(m.group(1))
    return {
        "title": ed.get("title") or meta.get("title"),
        "authors": [],  # edition authors are keys; fill from search if needed
        "publicationYear": year,
        "pageCount": ed.get("number_of_pages"),
        "isbn13": re.sub(r"[^0-9X]", "", isbn) if len(re.sub(r"[^0-9X]", "", isbn)) == 13 else None,
        "olKey": work_key,
        "synopsis": meta.get("synopsis"),
    }


def _by_title(title: str) -> dict | None:
    try:
        r = requests.get(
            f"{OL}/search.json",
            params={"title": title, "limit": 1,
                    "fields": "key,title,author_name,first_publish_year,isbn,number_of_pages_median"},
            timeout=20,
        )
        docs = (r.json().get("docs") or []) if r.ok else []
    except Exception:  # noqa: BLE001
        return None
    if not docs:
        return None
    d = docs[0]
    meta = _work_meta(d.get("key"))
    return {
        "title": d.get("title"),
        "authors": d.get("author_name") or [],
        "publicationYear": d.get("first_publish_year"),
        "pageCount": d.get("number_of_pages_median"),
        "isbn13": _isbn13(d.get("isbn")),
        "olKey": d.get("key"),
        "synopsis": meta.get("synopsis"),
    }


def lookup(title: str | None = None, isbn: str | None = None) -> dict | None:
    """Return a best-guess metadata dict, or None if nothing matched."""
    meta = _by_isbn(isbn) if isbn else None
    if not meta and title:
        meta = _by_title(title)
    # If ISBN gave us a work but no authors, backfill from a title search.
    if meta and not meta.get("authors") and meta.get("title"):
        t = _by_title(meta["title"])
        if t:
            meta["authors"] = t.get("authors") or []
    return meta
