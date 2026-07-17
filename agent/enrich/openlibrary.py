"""Open Library client — one consolidated surface for the enrichment loop.

Replaces the scattered OL access (``agent/club/openlibrary.py`` add-book lookup +
``corpus/openlibrary_subjects.py`` subject backfill). It resolves a book to its OL
Work, pulls book facts (description, subjects, ratings, editions, cover, ISBN, year,
pages) and the work's author keys, and fetches OL *author* records (bio, birth/death,
photo, Wikidata link) — the author fetcher documented in CLAUDE.md but never built.

All fetches are best-effort via ``agent.enrich.http`` (None on failure).
"""

from __future__ import annotations

import re

from agent.enrich import http

OL = "https://openlibrary.org"
COVERS = "https://covers.openlibrary.org"

# Generic subject tags that don't discriminate a ~180-book corpus (we already have
# a topic taxonomy + fiction flag). Salvaged from corpus/openlibrary_subjects.py.
SKIP_TAGS = {
    "fiction",
    "nonfiction",
    "non-fiction",
    "english language",
    "literature",
    "general",
    "audiobook",
    "open library staff picks",
    "popular print",
    "accessible book",
    "protected daisy",
    "in library",
    "books",
    "history",
}
MAX_SUBJECTS = 12
MAX_TAG_LEN = 80


# ── Subjects ─────────────────────────────────────────────────────────────────
def clean_subjects(raw) -> list[str]:
    """Normalize, dedupe, drop generic tags, cap to MAX_SUBJECTS."""
    seen: set[str] = set()
    out: list[str] = []
    for tag in raw or []:
        if not isinstance(tag, str):
            continue
        clean = tag.strip()
        if not clean or len(clean) > MAX_TAG_LEN:
            continue
        norm = clean.lower()
        if norm in seen or norm in SKIP_TAGS:
            continue
        seen.add(norm)
        out.append(clean)
        if len(out) >= MAX_SUBJECTS:
            break
    return out


# ── Resolver (title → Work) ──────────────────────────────────────────────────
def _author_matches(doc_authors: list[str], our_authors: list[str]) -> bool:
    """Loose author-name match: last-name overlap. Guards against picking a
    same-titled book by a different author (the "A World Appears" trap)."""
    if not our_authors:
        return True
    doc_lower = " ".join(a.lower() for a in doc_authors or [])
    if not doc_lower:
        return False
    for ours in our_authors:
        last = ours.lower().split()[-1] if ours else ""
        if last and last in doc_lower:
            return True
    return False


# Rich field set so one search doc yields ratings/editions/cover/ids in a single call.
_SEARCH_FIELDS = (
    "key,title,subject,author_name,author_key,first_publish_year,"
    "number_of_pages_median,isbn,edition_count,cover_i,"
    "ratings_average,ratings_count,language"
)


def search_best_match(title: str, authors: list[str]) -> dict | None:
    """Search OL for the right Work doc, requiring an author match when we have
    authors. Search is relevance-ordered, so the first author-matching hit wins;
    among matches, prefer ones carrying subjects. Falls back through query shapes."""
    if not title:
        return None
    queries: list[dict] = []
    if authors:
        queries.append({"title": title, "author": authors[0]})
    queries.append({"title": title})
    queries.append({"q": f"{title} {authors[0] if authors else ''}".strip()})

    for params in queries:
        params.update({"limit": 10, "fields": _SEARCH_FIELDS})
        data = http.get_json(f"{OL}/search.json", params=params)
        docs = (data or {}).get("docs") or []
        candidates = [d for d in docs if _author_matches(d.get("author_name") or [], authors)]
        with_subjects = [d for d in candidates if d.get("subject")]
        if with_subjects:
            return with_subjects[0]
        if candidates:
            return candidates[0]
    return None


def _isbn13(values) -> str | None:
    for v in values or []:
        digits = re.sub(r"[^0-9X]", "", str(v))
        if len(digits) == 13:
            return digits
    return None


def _year(text) -> int | None:
    m = re.search(r"\b(1[0-9]\d\d|20\d\d)\b", str(text or ""))
    return int(m.group(1)) if m else None


# ── Work + editions ──────────────────────────────────────────────────────────
def work(ol_key: str | None) -> dict | None:
    """Fetch an OL Work record (/works/OL..W.json)."""
    if not ol_key:
        return None
    return http.get_json(f"{OL}{ol_key}.json")


def _work_description(w: dict) -> str | None:
    desc = w.get("description")
    if isinstance(desc, dict):
        desc = desc.get("value")
    return (desc or None) and str(desc).strip() or None


def _work_author_keys(w: dict) -> list[str]:
    keys = []
    for a in w.get("authors") or []:
        key = (a.get("author") or {}).get("key") if isinstance(a, dict) else None
        if key:
            keys.append(key)
    return keys


def editions(ol_key: str | None, limit: int = 50) -> dict:
    """Aggregate edition-level facts: languages seen and edition count."""
    if not ol_key:
        return {}
    data = http.get_json(f"{OL}{ol_key}/editions.json", params={"limit": limit})
    entries = (data or {}).get("entries") or []
    langs: list[str] = []
    for ed in entries:
        for lang in ed.get("languages") or []:
            code = (lang.get("key") or "").rsplit("/", 1)[-1]
            if code and code not in langs:
                langs.append(code)
    return {
        "edition_count": (data or {}).get("size") or (len(entries) or None),
        "languages": langs,
    }


# ── Authors ──────────────────────────────────────────────────────────────────
def author(ol_author_key: str | None) -> dict | None:
    """Fetch an OL Author record (/authors/OL..A.json)."""
    if not ol_author_key:
        return None
    return http.get_json(f"{OL}{ol_author_key}.json")


def author_facts(rec: dict | None) -> dict:
    """Extract usable author fields from an OL Author record."""
    if not rec:
        return {}
    bio = rec.get("bio")
    if isinstance(bio, dict):
        bio = bio.get("value")
    links = rec.get("links") or []
    website = None
    for ln in links:
        url = (ln.get("url") or "") if isinstance(ln, dict) else ""
        if url.startswith("http"):
            website = url
            break
    remote = rec.get("remote_ids") or {}
    photos = [p for p in (rec.get("photos") or []) if isinstance(p, int) and p > 0]
    return {
        "bio": (bio or None) and str(bio).strip() or None,
        "birth_year": _year(rec.get("birth_date")),
        "death_year": _year(rec.get("death_date")),
        "website": website,
        "wikidata_id": remote.get("wikidata"),
        "ol_photo_id": photos[0] if photos else None,
    }


def resolve_author_key(work_rec: dict | None, name: str) -> str | None:
    """Pick the OL author key from a Work's authors that matches `name` (by last
    name). With a single author the match is trivial; with several we guard."""
    keys = _work_author_keys(work_rec or {})
    if not keys:
        return None
    if len(keys) == 1:
        return keys[0]
    last = name.lower().split()[-1] if name else ""
    for key in keys:
        rec = author(key)
        if rec and last and last in (rec.get("name") or "").lower():
            return key
    return keys[0]


# ── Compose book facts ───────────────────────────────────────────────────────
def book_facts(title: str, authors: list[str], ol_key: str | None, isbn13: str | None) -> dict:
    """Resolve a book's OL Work and return enrichment fields + identifiers.

    Returns a dict with: ol_key, ol_cover_id, edition_count, languages, subjects,
    synopsis, publication_year, page_count, isbn13, ratings_average, ratings_count,
    author_keys (OL author keys from the Work)."""
    doc = search_best_match(title, authors)
    resolved_key = ol_key or (doc or {}).get("key")
    w = work(resolved_key) or {}
    ed = editions(resolved_key)

    covers = [c for c in (w.get("covers") or []) if isinstance(c, int) and c > 0]
    cover_id = covers[0] if covers else (doc or {}).get("cover_i")

    subjects = clean_subjects(w.get("subjects")) or clean_subjects((doc or {}).get("subject"))

    return {
        "ol_key": resolved_key,
        "ol_cover_id": cover_id,
        "edition_count": ed.get("edition_count") or (doc or {}).get("edition_count"),
        "languages": ed.get("languages") or [],
        "subjects": subjects,
        "synopsis": _work_description(w),
        "publication_year": (doc or {}).get("first_publish_year"),
        "page_count": (doc or {}).get("number_of_pages_median"),
        "isbn13": isbn13 or _isbn13((doc or {}).get("isbn")),
        "ratings_average": round(r, 2) if (r := (doc or {}).get("ratings_average")) else None,
        "ratings_count": (doc or {}).get("ratings_count"),
        "author_keys": _work_author_keys(w),
    }


# ── Cover / photo URLs ───────────────────────────────────────────────────────
def cover_url(cover_id: int, size: str = "L") -> str:
    return f"{COVERS}/b/id/{cover_id}-{size}.jpg"


def author_photo_url(ol_photo_id: int, size: str = "L") -> str:
    return f"{COVERS}/a/id/{ol_photo_id}-{size}.jpg"
