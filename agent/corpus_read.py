"""Read/query layer over the normalized Git corpus for Oliver's tools.

The corpus is normalized: book files are intrinsic + picker (member slugs); meetings
own date + book refs; reviews/awards reference by slug. This module mirrors the
website's build-time joins — it enriches books with their meeting date, picker names,
placeholder, etc. — so the query functions return the same shapes as before. Reads
fresh from disk each call (the corpus is small).
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
        return (yaml.safe_load(fm) or {}), (rest[0] if rest else "").strip()
    return {}, text.strip()


def members() -> list[dict]:
    return _load_json_dir("members")


def meetings() -> list[dict]:
    return _load_json_dir("meetings")


def reviews() -> list[dict]:
    d = DATA_DIR / "reviews"
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.md")):
        data, body = _parse_frontmatter(p.read_text())
        data["review"] = body or None
        out.append(data)
    return out


def _earliest_meeting_by_book() -> dict[str, dict]:
    m: dict[str, dict] = {}
    for mt in meetings():
        for bslug in mt.get("books") or []:
            cur = m.get(bslug)
            if not cur or (mt.get("date") or "") < (cur.get("date") or ""):
                m[bslug] = mt
    return m


def books() -> list[dict]:
    """Books enriched with their derived meeting + picker fields (keeps `picker` too)."""
    mbs = _earliest_meeting_by_book()
    member_by_slug = {m["slug"]: m for m in members()}
    out = []
    for b in _load_json_dir("books"):
        mt = mbs.get(b["slug"])
        md = mt.get("date") if mt else None
        pnames, pslugs = [], []
        for ps in b.get("picker") or []:
            mem = member_by_slug.get(ps)
            if not mem:
                continue
            pnames.append(mem["name"])
            pslugs.append(mem["slug"] if mem.get("isCurrent") else None)
        eb = dict(b)
        eb.update({
            "meetingDate": md,
            "year": int(md[:4]) if md else None,
            "placeholder": bool(mt.get("placeholder")) if mt else False,
            "meetingNotes": (mt.get("notes") if mt else None),
            "meetingLocation": (mt.get("location") if mt else None),
            "pickerName": pnames[0] if pnames else None,
            "pickerSlug": pslugs[0] if pslugs else None,
            "pickerNames": pnames or None,
            "pickerSlugs": pslugs or None,
        })
        out.append(eb)
    return out


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


def find_book(slug_or_title: str) -> dict | None:
    key = _norm(slug_or_title)
    bs = books()
    for b in bs:
        if _norm(b.get("slug")) == key:
            return b
    for b in bs:
        if _norm(b.get("title")) == key:
            return b
    for b in bs:
        if key in _norm(b.get("title")) or key in _norm(b.get("slug")):
            return b
    return None


def find_member(name_or_slug: str) -> dict | None:
    key = _norm(name_or_slug)
    for m in members():
        if _norm(m.get("slug")) == key or _norm(m.get("name")) == key:
            return m
    for m in members():
        if key and (key in _norm(m.get("name")) or key in _norm(m.get("slug"))):
            return m
    return None


def _reviews_for(*, book_slug: str | None = None, member_slug: str | None = None) -> list[dict]:
    titles = {b["slug"]: b.get("title") for b in _load_json_dir("books")}
    names = {m["slug"]: m.get("name") for m in members()}
    out = []
    for r in reviews():
        if book_slug and r.get("book") != book_slug:
            continue
        if member_slug and r.get("member") != member_slug:
            continue
        out.append({
            "book": titles.get(r.get("book")),
            "by": names.get(r.get("member")),
            "rating": r.get("rating"),
            "dnf": bool(r.get("dnf")),
            "wouldRecommend": bool(r.get("wouldRecommend")),
            "discussionQuality": r.get("discussionQuality"),
            "favoriteQuote": r.get("favoriteQuote"),
            "review": r.get("review"),
        })
    return out


def get_book(slug_or_title: str) -> dict | None:
    b = find_book(slug_or_title)
    if not b:
        return None
    brief = _book_brief(b)
    brief["synopsis"] = b.get("synopsis")
    brief["isbn13"] = b.get("isbn13")
    brief["meetingDate"] = b.get("meetingDate")
    brief["meetingNotes"] = b.get("meetingNotes")
    brief["reviews"] = _reviews_for(book_slug=b.get("slug"))
    return brief


def member_history(name_or_slug: str) -> dict | None:
    m = find_member(name_or_slug)
    if not m:
        return None
    picked = [b for b in books() if m["slug"] in (b.get("picker") or [])]
    picked.sort(key=lambda b: b.get("meetingDate") or "", reverse=True)
    return {
        "name": m.get("name"),
        "slug": m.get("slug"),
        "isCurrent": bool(m.get("isCurrent")),
        "website": m.get("website"),
        "pickedCount": len(picked),
        "picks": [{"title": b.get("title"), "year": b.get("year")} for b in picked],
        "reviews": _reviews_for(member_slug=m.get("slug")),
    }


def upcoming_meetings() -> list[dict]:
    future = [b for b in books() if b.get("placeholder")]
    future.sort(key=lambda b: b.get("meetingDate") or "")
    return [
        {"title": b.get("title"), "authors": b.get("authors") or [],
         "meetingDate": b.get("meetingDate"), "pickedBy": b.get("pickerName"),
         "topic": b.get("topic")}
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


def pending_reviews(name_or_slug: str) -> dict | None:
    m = find_member(name_or_slug)
    if not m:
        return None
    reviewed = {r.get("book") for r in reviews() if r.get("member") == m["slug"]}
    read = [b for b in books() if b.get("meetingDate") and not b.get("placeholder")]
    pending = [_book_brief(b) for b in read if b.get("slug") not in reviewed]
    pending.sort(key=lambda x: x["yearRead"] or 0, reverse=True)
    return {"member": m["name"], "count": len(pending), "books": pending}


def book_choices(prefix: str, limit: int = 25) -> list[tuple[str, str]]:
    p = _norm(prefix)
    out: list[tuple[str, str]] = []
    for b in sorted(books(), key=lambda x: (x.get("year") or 0), reverse=True):
        title = b.get("title") or ""
        if not p or p in title.lower():
            out.append((title, b.get("slug")))
        if len(out) >= limit:
            break
    return out
