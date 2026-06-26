"""Wikidata client — structured facts for authors and books, with verification.

The hard part is resolution: a bare name search can return the wrong same-named
entity. We defend against that by (a) preferring the Wikidata link Open Library
already stores on the author record, and (b) otherwise verifying the candidate's
type (P31) and — for authors — a writing occupation (P106) and/or a birth-year
hint before trusting it. A low-confidence candidate is skipped, not guessed.

Facts come from ``Special:EntityData/{qid}.json``; human-readable labels for
referenced entities (nationality, notable works, awards) are resolved in one
batched ``wbgetentities`` call.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from agent.enrich import http

API = "https://www.wikidata.org/w/api.php"
ENTITYDATA = "https://www.wikidata.org/wiki/Special:EntityData"
COMMONS_FILEPATH = "https://commons.wikimedia.org/wiki/Special:FilePath"

HUMAN = "Q5"
# Written-work types (for book verification).
WRITTEN_WORK = {
    "Q571",      # book
    "Q7725634",  # literary work
    "Q47461344", # written work
    "Q8261",     # novel
    "Q49084",    # short story
    "Q1279564",  # short-story collection
    "Q23927052", # essay
}
# Writing-related occupations (for author verification).
WRITER_OCCUPATIONS = {
    "Q36180",    # writer
    "Q482980",   # author
    "Q6625963",  # novelist
    "Q49757",    # poet
    "Q201788",   # historian
    "Q1930187",  # journalist
    "Q11774202", # essayist
    "Q28389",    # screenwriter
    "Q4853732",  # children's writer
    "Q18844224", # science-fiction writer
    "Q15980158", # non-fiction writer
    "Q214917",   # playwright
}


# ── Low-level API ────────────────────────────────────────────────────────────
def search(name: str, limit: int = 8) -> list[str]:
    """Candidate Q-ids for a name, relevance-ranked."""
    if not name:
        return []
    data = http.get_json(API, params={
        "action": "wbsearchentities", "search": name, "language": "en",
        "format": "json", "type": "item", "limit": limit,
    })
    return [r["id"] for r in (data or {}).get("search") or [] if r.get("id")]


def entity(qid: str | None) -> dict | None:
    """Fetch a single Wikidata entity (claims + sitelinks + labels)."""
    if not qid:
        return None
    data = http.get_json(f"{ENTITYDATA}/{qid}.json")
    return ((data or {}).get("entities") or {}).get(qid)


def labels(qids: list[str]) -> dict[str, str]:
    """Resolve Q-ids → English labels in one batched call."""
    ids = [q for q in dict.fromkeys(qids) if q]
    if not ids:
        return {}
    out: dict[str, str] = {}
    for i in range(0, len(ids), 50):  # API caps at 50 ids
        chunk = ids[i:i + 50]
        data = http.get_json(API, params={
            "action": "wbgetentities", "ids": "|".join(chunk),
            "props": "labels", "languages": "en", "format": "json",
            # Some items store the English label as 'mul' (multilingual); fallback
            # surfaces it under en. Without this they come back with empty labels.
            "languagefallback": "1",
        })
        for qid, ent in ((data or {}).get("entities") or {}).items():
            label = ((ent.get("labels") or {}).get("en") or {}).get("value")
            if label:
                out[qid] = label
    return out


# ── Claim helpers ────────────────────────────────────────────────────────────
def _claims(ent: dict, prop: str) -> list:
    return (ent.get("claims") or {}).get(prop) or []


def _values(ent: dict, prop: str) -> list:
    out = []
    for c in _claims(ent, prop):
        val = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if val is not None:
            out.append(val)
    return out


def _ids(ent: dict, prop: str) -> list[str]:
    return [v["id"] for v in _values(ent, prop) if isinstance(v, dict) and v.get("id")]


def _first_str(ent: dict, prop: str) -> str | None:
    for v in _values(ent, prop):
        if isinstance(v, str):
            return v
    return None


def _year_from_time(ent: dict, prop: str) -> int | None:
    for v in _values(ent, prop):
        if isinstance(v, dict) and v.get("time"):
            m = re.search(r"([+-])(\d{1,4})-", v["time"])
            if m:
                year = int(m.group(2))
                return -year if m.group(1) == "-" else year
    return None


def enwiki(ent: dict) -> tuple[str | None, str | None]:
    """(wikipedia title, wikipedia url) from the enwiki sitelink, if any."""
    sl = (ent.get("sitelinks") or {}).get("enwiki") or {}
    title = sl.get("title")
    if not title:
        return None, None
    url = sl.get("url") or f"https://en.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"
    return title, url


def commons_image_url(filename: str | None, width: int = 600) -> str | None:
    if not filename:
        return None
    return f"{COMMONS_FILEPATH}/{quote(filename)}?width={width}"


# ── Resolution (verified) ────────────────────────────────────────────────────
def resolve_author(name: str, *, ol_wikidata: str | None = None,
                   birth_year_hint: int | None = None) -> dict | None:
    """Return the verified author entity, or None. Trusts an OL→Wikidata link;
    otherwise requires P31=human plus a writing occupation and/or birth-year match."""
    if ol_wikidata:
        ent = entity(ol_wikidata)
        if ent and HUMAN in _ids(ent, "P31"):
            return ent
    for qid in search(name):
        ent = entity(qid)
        if not ent or HUMAN not in _ids(ent, "P31"):
            continue
        occ = set(_ids(ent, "P106"))
        has_writer_occ = bool(occ & WRITER_OCCUPATIONS)
        birth = _year_from_time(ent, "P569")
        birth_matches = birth_year_hint and birth and abs(birth - birth_year_hint) <= 1
        if has_writer_occ or birth_matches:
            return ent
    return None


def resolve_book(title: str, authors: list[str]) -> dict | None:
    """Return the verified written-work entity, or None. Requires a written-work
    type and an author (P50) whose label last-name matches one of ours."""
    if not title:
        return None
    last_names = {a.lower().split()[-1] for a in authors if a}
    for qid in search(title):
        ent = entity(qid)
        if not ent or not (set(_ids(ent, "P31")) & WRITTEN_WORK):
            continue
        if not last_names:
            return ent  # no author constraint available
        author_ids = _ids(ent, "P50")
        author_labels = labels(author_ids)
        # Whole-word match on the author labels' tokens (not substring) so a short/common
        # last name like "Ford" can't match "Crawford"/"Stafford" on a same-titled work.
        author_tokens = {t.strip(".,") for lbl in author_labels.values() for t in lbl.lower().split()}
        if any(ln in author_tokens for ln in last_names):
            return ent
    return None


# ── Fact extraction ──────────────────────────────────────────────────────────
def author_facts(ent: dict | None) -> dict:
    if not ent:
        return {}
    nationality_id = (_ids(ent, "P27") or [None])[0]
    notable_ids = _ids(ent, "P800")[:6]
    award_ids = _ids(ent, "P166")[:10]
    label_map = labels([nationality_id, *notable_ids, *award_ids])
    title, url = enwiki(ent)
    return {
        "wikidata_id": ent.get("id"),
        "birth_year": _year_from_time(ent, "P569"),
        "death_year": _year_from_time(ent, "P570"),
        "nationality": label_map.get(nationality_id) if nationality_id else None,
        "notable_works": [label_map[q] for q in notable_ids if q in label_map],
        "awards": [label_map[q] for q in award_ids if q in label_map],
        "website": _first_str(ent, "P856"),
        "goodreads_id": _first_str(ent, "P2963"),  # Goodreads author id
        "image_filename": _first_str(ent, "P18"),
        "wikipedia_title": title,
        "wikipedia_url": url,
    }


def book_facts(ent: dict | None) -> dict:
    if not ent:
        return {}
    award_ids = _ids(ent, "P166")[:10]
    series_ids = _ids(ent, "P179")[:1]
    label_map = labels([*award_ids, *series_ids])
    title, url = enwiki(ent)
    return {
        "wikidata_id": ent.get("id"),
        "awards": [label_map[q] for q in award_ids if q in label_map],
        "series": next((label_map[q] for q in series_ids if q in label_map), None),
        "goodreads_id": _first_str(ent, "P2969"),  # Goodreads book/work id
        "wikipedia_title": title,
        "wikipedia_url": url,
    }
