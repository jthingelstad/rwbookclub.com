"""Read/query layer over the per-entity Git corpus for Oliver's tools.

Loads books/members/meetings/reviews/awards from corpus/data/ and exposes query
functions (search, get_book, member_history, upcoming_meetings, club_stats). Reads
fresh from disk each call — the corpus is small and this keeps Oliver current if the
files change. Returns plain dicts/lists; agent/tools.py formats them for the model.
"""

from __future__ import annotations

import json
from collections import Counter

import yaml

from corpus.airtable import DATA_DIR


def _load_json_dir(name: str) -> list[dict]:
    d = DATA_DIR / name
    if not d.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))]


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        _, fm, *rest = text.split("---", 2)
        body = rest[0] if rest else ""
        return (yaml.safe_load(fm) or {}), body.strip()
    return {}, text.strip()


def _load_reviews() -> list[dict]:
    d = DATA_DIR / "reviews"
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.md")):
        data, body = _parse_frontmatter(p.read_text())
        data["review"] = body or None
        out.append(data)
    return out


def books() -> list[dict]:
    return _load_json_dir("books")


def members() -> list[dict]:
    return _load_json_dir("members")


def reviews() -> list[dict]:
    return _load_reviews()


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def _book_brief(b: dict) -> dict:
    return {
        "slug": b.get("slug"),
        "title": b.get("title"),
        "subtitle": b.get("subtitle"),
        "authors": b.get("authors") or [],
        "topic": b.get("topic"),
        "fiction": bool(b.get("fiction")),
        "publicationYear": b.get("publicationYear"),
        "pageCount": b.get("pageCount"),
        "yearRead": b.get("year"),
        "pickedBy": b.get("pickerName"),
        "placeholder": bool(b.get("placeholder")),
    }


# ── Queries (each maps to a tool) ────────────────────────────────────────────
def search_books(query: str | None = None, topic: str | None = None,
                 fiction: bool | None = None, year: int | None = None,
                 author: str | None = None, limit: int = 25) -> list[dict]:
    q, a = _norm(query), _norm(author)
    out = []
    for b in books():
        if topic and _norm(b.get("topic")) != _norm(topic):
            continue
        if fiction is not None and bool(b.get("fiction")) != fiction:
            continue
        if year is not None and b.get("year") != year and b.get("publicationYear") != year:
            continue
        if a and not any(a in _norm(x) for x in (b.get("authors") or [])):
            continue
        if q:
            hay = " ".join([
                b.get("title") or "", b.get("subtitle") or "", b.get("synopsis") or "",
                " ".join(b.get("authors") or []), b.get("topic") or "",
            ]).lower()
            if q not in hay:
                continue
        out.append(_book_brief(b))
    out.sort(key=lambda x: (x["yearRead"] or 0), reverse=True)
    return out[:limit]


def _find_book(slug_or_title: str) -> dict | None:
    key = _norm(slug_or_title)
    bs = books()
    for b in bs:  # exact slug
        if _norm(b.get("slug")) == key:
            return b
    for b in bs:  # exact title
        if _norm(b.get("title")) == key:
            return b
    for b in bs:  # contains
        if key in _norm(b.get("title")) or key in _norm(b.get("slug")):
            return b
    return None


def _reviews_for(*, book_id: str | None = None, member_id: str | None = None) -> list[dict]:
    titles = {b["id"]: b.get("title") for b in books()}
    out = []
    for r in reviews():
        if book_id and book_id not in (r.get("bookIds") or []):
            continue
        if member_id and member_id not in (r.get("memberIds") or []):
            continue
        names = [rv.get("name") for rv in (r.get("reviewers") or [])]
        book_titles = [titles.get(b) for b in (r.get("bookIds") or []) if titles.get(b)]
        out.append({
            "book": book_titles[0] if book_titles else None,
            "by": names,
            "rating": r.get("rating"),
            "dnf": bool(r.get("dnf")),
            "wouldRecommend": bool(r.get("wouldRecommend")),
            "discussionQuality": r.get("discussionQuality"),
            "favoriteQuote": r.get("favoriteQuote"),
            "review": r.get("review"),
        })
    return out


def get_book(slug_or_title: str) -> dict | None:
    b = _find_book(slug_or_title)
    if not b:
        return None
    brief = _book_brief(b)
    brief["synopsis"] = b.get("synopsis")
    brief["isbn13"] = b.get("isbn13")
    brief["meetingDate"] = b.get("meetingDate")
    brief["meetingNotes"] = b.get("meetingNotes")
    brief["reviews"] = _reviews_for(book_id=b.get("id"))
    return brief


def _find_member(name_or_slug: str) -> dict | None:
    key = _norm(name_or_slug)
    for m in members():
        if _norm(m.get("slug")) == key or _norm(m.get("name")) == key:
            return m
    for m in members():
        if key and (key in _norm(m.get("name")) or key in _norm(m.get("slug"))):
            return m
    return None


def member_history(name_or_slug: str) -> dict | None:
    m = _find_member(name_or_slug)
    if not m:
        return None
    return {
        "name": m.get("name"),
        "slug": m.get("slug"),
        "isCurrent": bool(m.get("isCurrent")),
        "website": m.get("website"),
        "pickedCount": m.get("pickedCount"),
        "picks": [
            {"title": p.get("title"), "year": p.get("year")}
            for p in (m.get("pickedBooks") or [])
        ],
        "reviews": _reviews_for(member_id=m.get("id")),
    }


def upcoming_meetings() -> list[dict]:
    future = [b for b in books() if b.get("placeholder")]
    future.sort(key=lambda b: b.get("meetingDate") or "")
    return [
        {
            "title": b.get("title"),
            "authors": b.get("authors") or [],
            "meetingDate": b.get("meetingDate"),
            "pickedBy": b.get("pickerName"),
            "topic": b.get("topic"),
        }
        for b in future
    ]


def club_stats() -> dict:
    read = [b for b in books() if b.get("meetingDate") and not b.get("placeholder")]
    topics = Counter(b.get("topic") or "Uncategorized" for b in read)
    years = Counter(b.get("year") for b in read if b.get("year"))
    pickers = Counter(b.get("pickerName") for b in read if b.get("pickerName"))
    pages = [b.get("pageCount") for b in read if b.get("pageCount")]
    pub_years = [b.get("publicationYear") for b in read if b.get("publicationYear")]
    fiction = sum(1 for b in read if b.get("fiction"))
    yrs = sorted(years)
    return {
        "totalRead": len(read),
        "fiction": fiction,
        "nonfiction": len(read) - fiction,
        "firstYear": yrs[0] if yrs else None,
        "lastYear": yrs[-1] if yrs else None,
        "totalPages": sum(pages),
        "avgPages": round(sum(pages) / len(pages)) if pages else 0,
        "topics": topics.most_common(),
        "booksByYear": sorted(years.items()),
        "pickerLeaderboard": pickers.most_common(),
        "oldestPublication": min(pub_years) if pub_years else None,
        "newestPublication": max(pub_years) if pub_years else None,
    }
