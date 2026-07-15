"""Wikidata client — structured facts for authors and books, with verification.

The hard part is resolution: a bare name search can return the wrong same-named
entity. We defend against that by corroborating a candidate against the Wikidata
author(s) of books the club has read, an Open Library link, or a birth-year hint.
A writing occupation is useful context, but is not identity evidence: same-named
screenwriters and authors are exactly the ambiguity this layer must reject. A
low-confidence candidate is skipped, not guessed.

Facts come from ``Special:EntityData/{qid}.json``; human-readable labels for
referenced entities (nationality, notable works, awards) are resolved in one
batched ``wbgetentities`` call.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
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
@dataclass(frozen=True)
class AuthorResolution:
    """A selected entity plus the evidence that made the identity safe to use."""

    entity: dict | None
    source: str | None
    evidence: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    considered_candidates: int = 0

    def diagnostics(self) -> dict:
        return {
            "selectedQid": self.entity.get("id") if self.entity else None,
            "source": self.source,
            "evidence": list(self.evidence),
            "warnings": list(self.warnings),
            "consideredCandidates": self.considered_candidates,
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
def _name_tokens(value: str | None) -> tuple[str, ...]:
    """Comparable name tokens, ignoring accents, punctuation, and middle initials."""
    normalized = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    tokens = re.findall(r"[a-z0-9]+", normalized.lower())
    return tuple(token for token in tokens if len(token) > 1)


def _entity_name(ent: dict) -> str | None:
    label = ((ent.get("labels") or {}).get("en") or {}).get("value")
    if label:
        return label
    title, _ = enwiki(ent)
    return title.split(" (")[0] if title else None


def _author_ids_for_works(work_qids: list[str]) -> set[str]:
    author_ids: set[str] = set()
    for qid in dict.fromkeys(work_qids):
        work = entity(qid)
        if work:
            author_ids.update(_ids(work, "P50"))
    return author_ids


def resolve_author(
    name: str,
    *,
    ol_wikidata: str | None = None,
    birth_year_hint: int | None = None,
    known_work_qids: list[str] | None = None,
) -> AuthorResolution:
    """Resolve an author only when independent identity evidence corroborates it.

    Book authorship is strongest: when one of the club's known work entities names
    its author(s), a candidate must be one of them. Otherwise an exact compatible
    name plus an Open Library link or matching birth year is required. A mere
    writing occupation never selects a same-named person.
    """
    candidate_sources: dict[str, str] = {}
    if ol_wikidata:
        candidate_sources[ol_wikidata] = "openlibrary"
    for qid in search(name):
        candidate_sources.setdefault(qid, "search")

    expected_author_ids = _author_ids_for_works(known_work_qids or [])
    eligible: list[tuple[int, dict, str, tuple[str, ...]]] = []
    plausible_but_uncorroborated = False
    for qid, source in candidate_sources.items():
        ent = entity(qid)
        if not ent or HUMAN not in _ids(ent, "P31"):
            continue
        if _name_tokens(_entity_name(ent)) != _name_tokens(name):
            continue

        evidence: list[str] = []
        if qid in expected_author_ids:
            evidence.append("known_work_author")
        birth = _year_from_time(ent, "P569")
        if (
            birth_year_hint is not None
            and birth is not None
            and abs(birth - birth_year_hint) <= 1
        ):
            evidence.append("birth_year")
        if source == "openlibrary":
            evidence.append("openlibrary_link")

        # When a known work supplies author IDs, it is decisive: conflicting
        # Open Library links and same-name search results are quarantined.
        if expected_author_ids and qid not in expected_author_ids:
            plausible_but_uncorroborated = True
            continue
        if not evidence:
            plausible_but_uncorroborated = True
            continue

        score = (
            (100 if "known_work_author" in evidence else 0)
            + (60 if "birth_year" in evidence else 0)
            + (30 if "openlibrary_link" in evidence else 0)
        )
        eligible.append((score, ent, source, tuple(evidence)))

    if eligible:
        _, ent, source, evidence = max(eligible, key=lambda item: item[0])
        return AuthorResolution(
            ent, source, evidence, considered_candidates=len(candidate_sources)
        )
    warnings = ("wikidata_identity_not_corroborated",) if plausible_but_uncorroborated else ()
    return AuthorResolution(
        None,
        None,
        warnings=warnings,
        considered_candidates=len(candidate_sources),
    )


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
