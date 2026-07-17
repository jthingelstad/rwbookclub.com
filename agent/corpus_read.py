"""Read/query layer over the normalized corpus (generated from SQLite, gitignored) for Oliver's tools.

The corpus is normalized: book files are intrinsic + picker (member slugs); meetings
own date + book refs; reviews/lists reference by slug. This module mirrors the
website's build-time joins — it enriches books with their meeting date, picker names,
placeholder, etc. — so the query functions return the same shapes as before. One immutable,
indexed snapshot is shared until the generated corpus changes on disk.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from statistics import mean

import yaml

from agent import clock, config
from corpus.paths import DATA_DIR


def _load_json_dir(name: str) -> list[dict]:
    d = DATA_DIR / name
    if not d.exists():
        return []
    # slug is the filename — derive it, don't store it.
    return [{**json.loads(p.read_text()), "slug": p.stem} for p in sorted(d.glob("*.json"))]


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a YAML-frontmatter Markdown doc into (frontmatter dict, body str).

    Public because the review-prefill path in commands.py reaches into this same
    parser — it needs to read review files the same way the loader does.
    """
    if text.startswith("---"):
        _, fm, *rest = text.split("---", 2)
        return (yaml.safe_load(fm) or {}), (rest[0] if rest else "").strip()
    return {}, text.strip()


def _load_reviews() -> list[dict]:
    d = DATA_DIR / "reviews"
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.md")):
        data, body = parse_frontmatter(p.read_text())
        data["review"] = body or None
        out.append(data)
    return out


@dataclass(frozen=True)
class CorpusSnapshot:
    """One coherent, indexed view of a generated corpus generation."""

    books: tuple[dict, ...]
    members: tuple[dict, ...]
    meetings: tuple[dict, ...]
    authors: tuple[dict, ...]
    lists: tuple[dict, ...]
    reviews: tuple[dict, ...]
    members_by_slug: dict[str, dict]
    book_titles_by_slug: dict[str, str | None]
    earliest_meeting_by_book: dict[str, dict]
    reviews_by_book: dict[str, tuple[dict, ...]]
    reviews_by_member: dict[str, tuple[dict, ...]]

    @classmethod
    def load(cls) -> "CorpusSnapshot":
        raw_books = tuple(_load_json_dir("books"))
        raw_members = tuple(_load_json_dir("members"))
        raw_meetings = tuple(_load_json_dir("meetings"))
        raw_authors = tuple(_load_json_dir("authors"))
        raw_lists = tuple(_load_json_dir("lists"))
        raw_reviews = tuple(_load_reviews())

        earliest: dict[str, dict] = {}
        for meeting in raw_meetings:
            for book_slug in meeting.get("books") or []:
                current = earliest.get(book_slug)
                if not current or (meeting.get("date") or "") < (current.get("date") or ""):
                    earliest[book_slug] = meeting

        by_book: defaultdict[str, list[dict]] = defaultdict(list)
        by_member: defaultdict[str, list[dict]] = defaultdict(list)
        for review in raw_reviews:
            if review.get("book"):
                by_book[review["book"]].append(review)
            if review.get("member"):
                by_member[review["member"]].append(review)

        return cls(
            books=raw_books,
            members=raw_members,
            meetings=raw_meetings,
            authors=raw_authors,
            lists=raw_lists,
            reviews=raw_reviews,
            members_by_slug={member["slug"]: member for member in raw_members},
            book_titles_by_slug={book["slug"]: book.get("title") for book in raw_books},
            earliest_meeting_by_book=earliest,
            reviews_by_book={key: tuple(value) for key, value in by_book.items()},
            reviews_by_member={key: tuple(value) for key, value in by_member.items()},
        )


_snapshot_cache: CorpusSnapshot | None = None
_snapshot_cache_sig: tuple | None = None
_books_cache: list[dict] | None = None
_books_cache_sig: tuple | None = None


def _corpus_signature() -> tuple:
    parts = []
    for sub, pattern in (
        ("books", "*.json"),
        ("members", "*.json"),
        ("meetings", "*.json"),
        ("authors", "*.json"),
        ("lists", "*.json"),
        ("reviews", "*.md"),
    ):
        directory = DATA_DIR / sub
        if not directory.exists():
            parts.append((sub, 0, 0, 0))
            continue
        files = list(directory.glob(pattern))
        stats = [path.stat() for path in files]
        parts.append(
            (
                sub,
                len(files),
                sum(stat.st_mtime_ns for stat in stats),
                sum(stat.st_size for stat in stats),
            )
        )
    return tuple(parts)


def snapshot() -> CorpusSnapshot:
    """Return the current corpus snapshot, rebuilding atomically after any on-disk change."""
    global _snapshot_cache, _snapshot_cache_sig
    signature = _corpus_signature()
    if _snapshot_cache is None or signature != _snapshot_cache_sig:
        _snapshot_cache = CorpusSnapshot.load()
        _snapshot_cache_sig = signature
        invalidate_books()
    return _snapshot_cache


def invalidate_books() -> None:
    """Clear fields derived from both corpus data and the moving club clock."""
    global _books_cache, _books_cache_sig
    _books_cache = None
    _books_cache_sig = None


def invalidate() -> None:
    """Drop all cached corpus state after a successful generation."""
    global _snapshot_cache, _snapshot_cache_sig
    _snapshot_cache = None
    _snapshot_cache_sig = None
    invalidate_books()


def members() -> list[dict]:
    return list(snapshot().members)


def human_current_members() -> list[dict]:
    """Current members minus Oliver — the club's HUMAN roster. Oliver has a real club_members row
    (the sixth member: public profile, webapp login), but human-only mechanics — roll calls,
    reading check-ins, outreach, contact audits, taste lenses — enumerate members through here so
    they never target the agent itself."""
    return [
        m for m in members() if m.get("isCurrent") and m.get("slug") != config.OLIVER_MEMBER_SLUG
    ]


def meetings() -> list[dict]:
    return list(snapshot().meetings)


def authors() -> list[dict]:
    return list(snapshot().authors)


def lists() -> list[dict]:
    return list(snapshot().lists)


def reviews() -> list[dict]:
    return list(snapshot().reviews)


def _earliest_meeting_by_book() -> dict[str, dict]:
    return snapshot().earliest_meeting_by_book


def _today_iso() -> str:
    """Today's date in the club's LOCAL timezone. See agent.clock."""
    return clock.club_today_iso()


def books() -> list[dict]:
    """Books enriched with their derived meeting + picker fields (keeps `picker` too).

    Cached on the corpus snapshot plus the current club-clock minute so a meeting rolls from
    upcoming to past in a long-running Oliver process without requiring a corpus rewrite.
    """
    global _books_cache, _books_cache_sig
    corpus = snapshot()
    sig = (_snapshot_cache_sig, clock.club_now().isoformat(timespec="minutes"))
    if _books_cache is not None and sig == _books_cache_sig:
        return _books_cache

    mbs = corpus.earliest_meeting_by_book
    member_by_slug = corpus.members_by_slug
    out = []
    for b in corpus.books:
        mt = mbs.get(b["slug"])
        md = mt.get("date") if mt else None
        # Upcoming vs past is derived purely from the meeting's local date+time (no placeholder
        # flag): a meeting is upcoming until start + buffer has elapsed (see agent.clock).
        is_upcoming = bool(mt and clock.is_upcoming(md, mt.get("startTime")))
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
        eb.update(
            {
                "meetingDate": md,
                "meetingStartTime": (mt.get("startTime") if mt else None),
                "year": int(md[:4]) if md else None,
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
            }
        )
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
        "isUpcoming": bool(b.get("isUpcoming")),
        "isRead": bool(b.get("isRead")),
        # OL subject tags — up to 5 — give Oliver thematic detail beyond the 11 topics.
        "subjects": subjects[:5] if subjects else None,
    }


def search_books(
    query: str | None = None,
    topic: str | None = None,
    fiction: bool | None = None,
    year: int | None = None,
    author: str | None = None,
    limit: int = 25,
) -> list[dict]:
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
            hay = " ".join(
                [
                    b.get("title") or "",
                    b.get("subtitle") or "",
                    b.get("synopsis") or "",
                    " ".join(b.get("authors") or []),
                    b.get("topic") or "",
                ]
            ).lower()
            if q not in hay:
                continue
        out.append(_book_brief(b))
    out.sort(key=lambda x: x["yearRead"] or 0, reverse=True)
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
            score += 50  # author substring
        topic_n = _norm(b.get("topic"))
        if q == topic_n:
            score += 80  # exact topic
        elif topic_n and q in topic_n:
            score += 25  # topic substring
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
                hits = sum(1 for st in subjects_norm if any(tok in st for tok in q_tokens))
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


def find_list(name_or_slug: str, *, owner_slug: str | None = None) -> dict | None:
    """A list by slug or (case-insensitive) name. If `owner_slug` is given, only that member's
    lists are considered (so a member resolves their own list by name)."""
    key = _norm(name_or_slug)
    candidates = [x for x in lists() if owner_slug is None or x.get("owner") == owner_slug]
    for x in candidates:
        if _norm(x.get("slug")) == key or _norm(x.get("name")) == key:
            return x
    for x in candidates:
        if key and (key in _norm(x.get("name")) or key in _norm(x.get("slug"))):
            return x
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
        {"slug": b["slug"], "title": b.get("title"), "year": b.get("year"), "topic": b.get("topic")}
        for b in books()
        if name and name in (b.get("authors") or [])
    ]
    read.sort(key=lambda x: x.get("year") or 0, reverse=True)
    lifespan = None
    if a.get("birthYear"):
        lifespan = (
            f"{a['birthYear']}–{a['deathYear']}" if a.get("deathYear") else f"b. {a['birthYear']}"
        )
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


def _reviews_for(*, book_slug: str | None = None, member_slug: str | None = None) -> list[dict]:
    corpus = snapshot()
    titles = corpus.book_titles_by_slug
    names = {slug: member.get("name") for slug, member in corpus.members_by_slug.items()}
    if book_slug:
        candidates = corpus.reviews_by_book.get(book_slug, ())
    elif member_slug:
        candidates = corpus.reviews_by_member.get(member_slug, ())
    else:
        candidates = corpus.reviews
    out = []
    for r in candidates:
        if book_slug and r.get("book") != book_slug:
            continue
        if member_slug and r.get("member") != member_slug:
            continue
        out.append(
            {
                "book": titles.get(r.get("book")),
                "by": names.get(r.get("member")),
                "rating": r.get("rating"),
                "dnf": bool(r.get("dnf")),
                "wouldRecommend": bool(r.get("wouldRecommend")),
                "discussionQuality": r.get("discussionQuality"),
                "favoriteQuote": r.get("favoriteQuote"),
                "review": r.get("review"),
            }
        )
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
    brief["host"] = b.get("meetingHostNames")  # who hosted the meeting (≠ pickedBy)
    brief["reviews"] = _reviews_for(book_slug=b.get("slug"))
    brief["lists"] = lists_for_book(b.get("slug"))  # club + member lists featuring this book
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
    discussions = [r["discussionQuality"] for r in rs if r.get("discussionQuality") is not None]
    recommends = [r for r in rs if r.get("wouldRecommend")]
    excerpts = []
    for r in rs:
        body = (r.get("review") or "").strip()
        if body:
            excerpts.append(
                {
                    "by": r.get("by"),
                    "rating": r.get("rating"),
                    "dnf": r.get("dnf"),
                    "excerpt": body[:320],
                }
            )
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
        t
        for t in _norm(" ".join([b.get("title") or "", b.get("synopsis") or ""])).split()
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
            t
            for t in _norm(
                " ".join([other.get("title") or "", other.get("synopsis") or ""])
            ).split()
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


def affinity_to_history(
    subjects: list[str],
    authors: list[str],
    *,
    title: str = "",
    synopsis: str = "",
    fiction: bool | None = None,
    limit: int = 5,
) -> list[dict]:
    """Score an ARBITRARY candidate (usually a book the club has NOT read) against every READ
    book, using related_books' exact weights — shared author +60, shared subjects min(n*12,48),
    fiction match +5, title/synopsis token overlap min(n*3,18) (no topic weight: external
    candidates carry OL subjects, not our topic taxonomy). Threshold 24, like the site's related
    scoring. Returns the nearest shelf-neighbors with reasons — the 'how does this sit against
    what we've read' evidence for pick evaluation."""
    base_subjects = {_norm(s) for s in (subjects or []) if s}
    base_authors = {_norm(a) for a in (authors or []) if a}
    base_tokens = {t for t in _norm(f"{title} {synopsis}").split() if len(t) > 4}
    scored = []
    for other in books():
        if not other.get("isRead"):
            continue
        score, reasons = 0, []
        other_authors = {_norm(a): a for a in (other.get("authors") or [])}
        shared_authors = sorted(other_authors[k] for k in base_authors & set(other_authors))
        if shared_authors:
            score += 60
            reasons.append("same author: " + ", ".join(shared_authors))
        other_subjects = {_norm(s): s for s in (other.get("subjects") or [])}
        shared_subjects = sorted(other_subjects[k] for k in base_subjects & set(other_subjects))
        if shared_subjects:
            score += min(len(shared_subjects) * 12, 48)
            reasons.append("shared subjects: " + ", ".join(shared_subjects[:3]))
        if fiction is not None and bool(other.get("fiction")) == fiction:
            score += 5
        other_tokens = {
            t
            for t in _norm(
                " ".join([other.get("title") or "", other.get("synopsis") or ""])
            ).split()
            if len(t) > 4
        }
        overlap = sorted(base_tokens & other_tokens)
        if overlap:
            score += min(len(overlap) * 3, 18)
            reasons.append("shared language: " + ", ".join(overlap[:3]))
        if score >= 24:
            scored.append((score, other, reasons))
    scored.sort(key=lambda x: (-x[0], -(x[1].get("year") or 0)))
    return [
        {
            "slug": o["slug"],
            "title": o.get("title"),
            "authors": o.get("authors") or [],
            "yearRead": (o.get("meetingDate") or "")[:4] or None,
            "picker": o.get("pickerName"),
            "score": score,
            "reasons": reasons[:3],
        }
        for score, o, reasons in scored[:limit]
    ]


def unread_notable_works(limit: int = 10) -> list[dict]:
    """Authors the club has read who have enrichment-known notable works the club has NOT read —
    concrete pick leads ('you loved Ishiguro twice; this one is sitting right there'). Each author
    is annotated with the club's verdicts on their read books."""
    read_titles = {_norm(b.get("title") or "") for b in books() if b.get("isRead")}
    out = []
    for a in authors():
        works = a.get("notableWorks") or []
        if not works:
            continue
        name = a.get("name")
        read_by_author = [
            b for b in books() if b.get("isRead") and name in (b.get("authors") or [])
        ]
        if not read_by_author:
            continue
        unread = [w for w in works if _norm(w) not in read_titles][:4]
        if not unread:
            continue
        verdicts = []
        for b in read_by_author:
            rs = review_summary(b["slug"]) or {}
            verdicts.append(
                {
                    "title": b.get("title"),
                    "yearRead": (b.get("meetingDate") or "")[:4],
                    "ratingAverage": rs.get("ratingAverage"),
                    "discussionAverage": rs.get("discussionAverage"),
                }
            )
        out.append(
            {
                "author": name,
                "readCount": len(read_by_author),
                "clubVerdicts": verdicts,
                "unreadNotableWorks": unread,
            }
        )
    # Most-read (best-known) authors first — those leads carry the most club evidence.
    out.sort(key=lambda x: (-x["readCount"], x["author"] or ""))
    return out[:limit]


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
        "missing": [ref for ref, book in zip(book_refs[:5], found, strict=True) if book is None],
        "sharedSubjects": shared_subjects[:10],
    }


def _book_titles_by_slug() -> dict[str, str | None]:
    return snapshot().book_titles_by_slug


def _hosted_meetings_for(member_slug: str) -> list[dict]:
    """Meetings a member hosted (most-recent first): {date, year, books[titles]}.
    Hosting is meeting-level (a host can run a 2-book meeting), so derive from meetings()."""
    titles = _book_titles_by_slug()
    out = [
        {
            "date": mt.get("date"),
            "year": int(mt["date"][:4]) if mt.get("date") else None,
            "books": [titles.get(s, s) for s in (mt.get("books") or [])],
        }
        for mt in meetings()
        if member_slug in (mt.get("host") or [])
    ]
    out.sort(key=lambda h: h.get("date") or "", reverse=True)
    return out


def lists_for_member(member_slug: str) -> list[dict]:
    """A member's book lists with entry book slugs resolved to titles, for display + Oliver."""
    titles = _book_titles_by_slug()
    out = []
    for x in lists():
        if x.get("owner") != member_slug:
            continue
        out.append(
            {
                "name": x.get("name"),
                "slug": x.get("slug"),
                "description": x.get("description"),
                "books": [
                    {"title": titles.get(e.get("book"), e.get("book")), "note": e.get("note")}
                    for e in (x.get("books") or [])
                ],
            }
        )
    return out


def lists_for_book(book_slug: str) -> list[dict]:
    """The club + member lists that feature this book (with the per-list note, if any)."""
    out = []
    for x in lists():
        for e in x.get("books") or []:
            if e.get("book") == book_slug:
                out.append(
                    {
                        "name": x.get("name"),
                        "slug": x.get("slug"),
                        "scope": x.get("scope"),
                        "owner": x.get("owner"),
                        "note": e.get("note"),
                    }
                )
                break
    return out


def member_history(name_or_slug: str) -> dict | None:
    m = find_member(name_or_slug)
    if not m:
        return None
    picked = [b for b in books() if m["slug"] in (b.get("picker") or [])]
    picked.sort(key=lambda b: b.get("meetingDate") or "", reverse=True)
    # Hosting — who ran the meeting (distinct from picking the book; usually the same person).
    hosted = _hosted_meetings_for(m["slug"])
    return {
        "name": m.get("name"),
        "slug": m.get("slug"),
        "isCurrent": bool(m.get("isCurrent")),
        "joined": m.get("joined"),
        "websites": m.get("websites") or [],
        "pickedCount": len(picked),
        "picks": [{"title": b.get("title"), "year": b.get("year")} for b in picked],
        "hostedCount": len(hosted),
        "hosted": hosted,
        "reviews": _reviews_for(member_slug=m.get("slug")),
        "lists": lists_for_member(m.get("slug")),
    }


def upcoming_meetings() -> list[dict]:
    """Meetings that haven't happened yet, earliest first. "Upcoming" is derived from the
    meeting's local date+time (see agent.clock): a meeting drops off once its start + buffer has
    passed. The predictive last-Tuesday schedule sets the dates; there is no placeholder flag."""
    future = [b for b in books() if b.get("isUpcoming")]
    future.sort(key=lambda b: b.get("meetingDate") or "")
    return [
        {
            "slug": b.get("slug"),
            "title": b.get("title"),
            "authors": b.get("authors") or [],
            "meetingDate": b.get("meetingDate"),
            "startTime": b.get("meetingStartTime"),
            "location": b.get("meetingLocation"),
            "pickedBy": b.get("pickerName"),
            "topic": b.get("topic"),
        }
        for b in future
    ]


def club_stats() -> dict:
    read = [b for b in books() if b.get("isRead")]
    topics = Counter(b.get("topic") or "Uncategorized" for b in read)
    years = Counter(b.get("year") for b in read if b.get("year"))
    pickers = Counter(b.get("pickerName") for b in read if b.get("pickerName"))
    # Hosting leaderboard — meetings hosted per member (meeting-level; ≠ picker leaderboard).
    name_by_slug = {mm["slug"]: mm.get("name") for mm in members()}
    hosts = Counter()
    for mt in meetings():
        for s in mt.get("host") or []:
            if name_by_slug.get(s):
                hosts[name_by_slug[s]] += 1
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
        "hostLeaderboard": hosts.most_common(),
        "oldestPublication": min(pub_years) if pub_years else None,
        "newestPublication": max(pub_years) if pub_years else None,
    }


def pending_reviews(name_or_slug: str) -> dict | None:
    m = find_member(name_or_slug)
    if not m:
        return None
    reviewed = {r.get("book") for r in reviews() if r.get("member") == m["slug"]}
    read = [b for b in books() if b.get("isRead")]
    joined = m.get("joined")
    if joined:  # a member owes nothing for books read before they joined (fail open on NULLs)
        read = [b for b in read if not b.get("meetingDate") or b["meetingDate"] >= joined]
    pending = [_book_brief(b) for b in read if b.get("slug") not in reviewed]
    pending.sort(key=lambda x: x["yearRead"] or 0, reverse=True)
    return {"member": m["name"], "count": len(pending), "books": pending}


def book_choices(prefix: str, limit: int = 25) -> list[tuple[str, str]]:
    p = _norm(prefix)
    out: list[tuple[str, str]] = []
    for b in sorted(books(), key=lambda x: x.get("year") or 0, reverse=True):
        title = b.get("title") or ""
        if not p or p in title.lower():
            out.append((title, b.get("slug")))
        if len(out) >= limit:
            break
    return out
