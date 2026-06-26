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
from datetime import datetime, timezone
from statistics import mean

import yaml

from corpus.paths import DATA_DIR


def _load_json_dir(name: str) -> list[dict]:
    d = DATA_DIR / name
    if not d.exists():
        return []
    # slug is the filename — derive it, don't store it.
    return [{**json.loads(p.read_text()), "slug": p.stem} for p in sorted(d.glob("*.json"))]


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a YAML-frontmatter Markdown doc into (frontmatter dict, body str).

    Public because reviews.py and bot.py reach into this same parser — both
    need to read review files the same way the loader does.
    """
    if text.startswith("---"):
        _, fm, *rest = text.split("---", 2)
        return (yaml.safe_load(fm) or {}), (rest[0] if rest else "").strip()
    return {}, text.strip()


def members() -> list[dict]:
    return _load_json_dir("members")


def meetings() -> list[dict]:
    return _load_json_dir("meetings")


def authors() -> list[dict]:
    return _load_json_dir("authors")


def awards() -> list[dict]:
    return _load_json_dir("awards")


def reviews() -> list[dict]:
    d = DATA_DIR / "reviews"
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.md")):
        data, body = parse_frontmatter(p.read_text())
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


# ── books() cache ────────────────────────────────────────────────────────────
# books() does ~375 file reads + a join, and Oliver's tools call it many times
# per turn (find_books → books(); get_book → find_book → books(); etc.). We
# cache the enriched list keyed on a signature (file count + max mtime) over
# the three dirs it composes from — any add, remove, or edit changes the
# signature and the cache rebuilds. Stat overhead is ~370 syscalls (<1ms);
# savings on a cache hit are the JSON parse + enrichment loop (~10–50ms).
_books_cache: list[dict] | None = None
_books_cache_sig: tuple | None = None


def _books_signature() -> tuple:
    parts = []
    for sub in ("books", "meetings", "members"):
        d = DATA_DIR / sub
        if not d.exists():
            parts.append((sub, 0, 0.0))
            continue
        files = list(d.glob("*.json"))
        mt = max((f.stat().st_mtime for f in files), default=0.0)
        parts.append((sub, len(files), mt))
    return tuple(parts)


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def books() -> list[dict]:
    """Books enriched with their derived meeting + picker fields (keeps `picker` too).

    Cached on a (count, max-mtime) signature over books/, meetings/, members/.
    """
    global _books_cache, _books_cache_sig
    sig = _books_signature()
    if _books_cache is not None and sig == _books_cache_sig:
        return _books_cache

    mbs = _earliest_meeting_by_book()
    member_by_slug = {m["slug"]: m for m in members()}
    today = _today_iso()
    out = []
    for b in _load_json_dir("books"):
        mt = mbs.get(b["slug"])
        md = mt.get("date") if mt else None
        is_upcoming = bool(mt and mt.get("placeholder") and (md or "")[:10] >= today)
        is_read = bool(md and not is_upcoming)
        pnames, pslugs = [], []
        for ps in b.get("picker") or []:
            mem = member_by_slug.get(ps)
            if not mem:
                continue
            pnames.append(mem["name"])
            pslugs.append(mem["slug"] if mem.get("isCurrent") else None)
        host_slugs = (mt.get("host") if mt else None) or []
        host_names = [member_by_slug[h]["name"] for h in host_slugs if h in member_by_slug]
        eb = dict(b)
        eb.update({
            "meetingDate": md,
            "meetingStartTime": (mt.get("startTime") if mt else None),
            "year": int(md[:4]) if md else None,
            "placeholder": bool(mt.get("placeholder")) if mt else False,
            "isUpcoming": is_upcoming,
            "isRead": is_read,
            "meetingNotes": (mt.get("notes") if mt else None),
            "meetingLocation": (mt.get("location") if mt else None),
            # Picker = who chose the book (book-level). Host = who ran the meeting where it
            # was discussed (meeting-level); usually the same person, but can differ.
            "pickerName": pnames[0] if pnames else None,
            "pickerSlug": pslugs[0] if pslugs else None,
            "pickerNames": pnames or None,
            "pickerSlugs": pslugs or None,
            "meetingHostNames": host_names or None,
            "meetingHostSlugs": host_slugs or None,
        })
        out.append(eb)
    _books_cache = out
    _books_cache_sig = sig
    return _books_cache


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def _book_brief(b: dict) -> dict:
    subjects = b.get("subjects") or []
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
        "isUpcoming": bool(b.get("isUpcoming")),
        "isRead": bool(b.get("isRead")),
        # OL subject tags — up to 5 — give Oliver thematic detail beyond the 11 topics.
        "subjects": subjects[:5] if subjects else None,
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


def find_books(query: str, limit: int = 15) -> list[dict]:
    """Multi-angle relevance search across the corpus, scored.

    Tries each book against the query as: exact author / exact topic / author
    substring / title substring / subtitle substring / topic substring /
    synopsis substring, and returns the best matches in one call. Use this
    for vague exploratory queries ("anything urban-planning related?",
    "long history stuff") so Oliver doesn't have to run 5-7 search_books
    variants. Returns [] for an empty query or no matches.
    """
    q = _norm(query)
    if not q:
        return []
    scored: dict[str, tuple[dict, int]] = {}
    for b in books():
        score = 0
        authors_norm = [_norm(a) for a in b.get("authors") or []]
        if any(q == a for a in authors_norm):
            score += 100  # exact author match
        elif any(q in a for a in authors_norm):
            score += 50   # author substring
        topic_n = _norm(b.get("topic"))
        if q == topic_n:
            score += 80   # exact topic
        elif topic_n and q in topic_n:
            score += 25   # topic substring
        if q in _norm(b.get("title")):
            score += 40
        if q in _norm(b.get("subtitle")):
            score += 30
        # OL subject tags — strong signal for thematic matching.
        subjects_norm = [_norm(s) for s in b.get("subjects") or []]
        if any(q == s for s in subjects_norm):
            score += 45  # exact subject match
        elif any(q in s for s in subjects_norm):
            score += 20  # subject substring
        else:
            # Token-level fallback for multi-word queries: "urban planning" should
            # surface books tagged "Urban economics" via the shared "urban" token.
            q_tokens = [t for t in q.split() if len(t) > 2]
            if q_tokens and subjects_norm:
                hits = sum(
                    1 for st in subjects_norm
                    if any(tok in st for tok in q_tokens)
                )
                if hits:
                    score += min(hits * 8, 24)
        if q in _norm(b.get("synopsis")):
            score += 10
        if score > 0:
            scored[b["slug"]] = (b, score)
    ordered = sorted(scored.values(), key=lambda x: (-x[1], -(x[0].get("year") or 0)))
    return [_book_brief(b) for b, _ in ordered[:limit]]


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


def find_author(name_or_slug: str) -> dict | None:
    key = _norm(name_or_slug)
    for a in authors():
        if _norm(a.get("slug")) == key or _norm(a.get("name")) == key:
            return a
    for a in authors():
        if key and (key in _norm(a.get("name")) or key in _norm(a.get("slug"))):
            return a
    return None


def get_author(name_or_slug: str) -> dict | None:
    """Author bio + the books the club has read by them."""
    a = find_author(name_or_slug)
    if not a:
        return None
    name = a.get("name")
    read = [
        {"slug": b["slug"], "title": b.get("title"), "year": b.get("year"),
         "topic": b.get("topic")}
        for b in books()
        if name and name in (b.get("authors") or [])
    ]
    read.sort(key=lambda x: x.get("year") or 0, reverse=True)
    lifespan = None
    if a.get("birthYear"):
        lifespan = f"{a['birthYear']}–{a['deathYear']}" if a.get("deathYear") else f"b. {a['birthYear']}"
    return {
        "name": name,
        "slug": a.get("slug"),
        "bio": a.get("bio"),
        # Enrichment (Open Library / Wikidata / Wikipedia) — lets Oliver actually
        # talk about the author, not just list their books.
        "birthYear": a.get("birthYear"),
        "deathYear": a.get("deathYear"),
        "lifespan": lifespan,
        "nationality": a.get("nationality"),
        "notableWorks": a.get("notableWorks"),
        "website": a.get("website"),
        "wikipediaUrl": a.get("wikipediaUrl"),
        "books": read,
        "bookCount": len(read),
    }


def awards_for_book(book_slug: str) -> list[dict]:
    return [
        {"name": a.get("name"), "year": a.get("year"),
         "award": a.get("award"), "notes": a.get("notes")}
        for a in awards()
        if book_slug in (a.get("books") or [])
    ]


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
    brief["awards"] = awards_for_book(b.get("slug"))  # the club's own awards
    # External enrichment (Open Library / Wikidata) — editions, reader ratings,
    # series, literary awards, and external links.
    brief["ratingsAverage"] = b.get("ratingsAverage")
    brief["ratingsCount"] = b.get("ratingsCount")
    brief["editionCount"] = b.get("editionCount")
    brief["series"] = b.get("series")
    brief["literaryAwards"] = b.get("awards")  # awards the book itself won (≠ club awards)
    brief["wikipediaUrl"] = b.get("wikipediaUrl")
    brief["goodreadsId"] = b.get("goodreadsId")
    brief["openLibraryKey"] = b.get("olKey")
    return brief


def review_summary(slug_or_title: str) -> dict | None:
    b = find_book(slug_or_title)
    if not b:
        return None
    rs = _reviews_for(book_slug=b["slug"])
    ratings = [r["rating"] for r in rs if r.get("rating") is not None and not r.get("dnf")]
    discussions = [
        r["discussionQuality"] for r in rs
        if r.get("discussionQuality") is not None
    ]
    recommends = [r for r in rs if r.get("wouldRecommend")]
    excerpts = []
    for r in rs:
        body = (r.get("review") or "").strip()
        if body:
            excerpts.append({
                "by": r.get("by"),
                "rating": r.get("rating"),
                "dnf": r.get("dnf"),
                "excerpt": body[:320],
            })
    return {
        "book": _book_brief(b),
        "reviewCount": len(rs),
        "ratingAverage": round(mean(ratings), 2) if ratings else None,
        "recommendCount": len(recommends),
        "dnfCount": sum(1 for r in rs if r.get("dnf")),
        "discussionAverage": round(mean(discussions), 2) if discussions else None,
        "excerpts": excerpts,
    }


def related_books(slug_or_title: str, limit: int = 8) -> dict | None:
    b = find_book(slug_or_title)
    if not b:
        return None
    base_subjects = set(b.get("subjects") or [])
    base_authors = set(b.get("authors") or [])
    base_tokens = {
        t for t in _norm(" ".join([b.get("title") or "", b.get("synopsis") or ""])).split()
        if len(t) > 4
    }
    scored = []
    for other in books():
        if other["slug"] == b["slug"]:
            continue
        score = 0
        reasons = []
        shared_authors = sorted(base_authors & set(other.get("authors") or []))
        if shared_authors:
            score += 60
            reasons.append("same author: " + ", ".join(shared_authors))
        if b.get("topic") and b.get("topic") == other.get("topic"):
            score += 35
            reasons.append(f"same topic: {b['topic']}")
        shared_subjects = sorted(base_subjects & set(other.get("subjects") or []))
        if shared_subjects:
            score += min(len(shared_subjects) * 12, 48)
            reasons.append("shared subjects: " + ", ".join(shared_subjects[:3]))
        if bool(b.get("fiction")) == bool(other.get("fiction")):
            score += 5
        other_tokens = {
            t for t in _norm(" ".join([other.get("title") or "", other.get("synopsis") or ""])).split()
            if len(t) > 4
        }
        overlap = sorted(base_tokens & other_tokens)
        if overlap:
            score += min(len(overlap) * 3, 18)
            reasons.append("shared language: " + ", ".join(overlap[:3]))
        if score:
            scored.append((score, other, reasons))
    scored.sort(key=lambda x: (-x[0], -(x[1].get("year") or 0)))
    return {
        "book": _book_brief(b),
        "related": [
            {**_book_brief(other), "score": score, "reasons": reasons[:3]}
            for score, other, reasons in scored[:limit]
        ],
    }


def compare_books(book_refs: list[str]) -> dict:
    found = [find_book(ref) for ref in book_refs[:5]]
    books_found = [b for b in found if b]
    subject_sets = [set(b.get("subjects") or []) for b in books_found]
    shared_subjects = sorted(set.intersection(*subject_sets)) if subject_sets else []
    return {
        "books": [
            {
                **_book_brief(b),
                "meetingDate": b.get("meetingDate"),
                "synopsis": b.get("synopsis"),
                "reviewSummary": review_summary(b["slug"]),
            }
            for b in books_found
        ],
        "missing": [
            ref for ref, book in zip(book_refs[:5], found)
            if book is None
        ],
        "sharedSubjects": shared_subjects[:10],
    }


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
    """Placeholder (= approximate-date / not-yet-confirmed) meetings that haven't
    happened yet. We filter past placeholders out because the placeholder flag
    is doing double-duty for "approximate date" and "future" — if the meeting
    date has passed, it's no longer upcoming regardless of whether someone
    flipped the flag yet."""
    future = [
        b for b in books()
        if b.get("isUpcoming")
    ]
    future.sort(key=lambda b: b.get("meetingDate") or "")
    return [
        {"title": b.get("title"), "authors": b.get("authors") or [],
         "meetingDate": b.get("meetingDate"), "pickedBy": b.get("pickerName"),
         "topic": b.get("topic")}
        for b in future
    ]


def club_stats() -> dict:
    read = [b for b in books() if b.get("isRead")]
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
    read = [b for b in books() if b.get("isRead")]
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
