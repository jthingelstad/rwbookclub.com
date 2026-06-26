"""Wikipedia REST summary — bio/description extract + portrait thumbnail.

Salvaged from ``corpus/wikipedia_author_bios.py`` (incl. its OVERRIDES for known
bare-name mis-matches). Resolution preference: an exact Wikipedia title from the
Wikidata sitelink (best — no guessing), else the bare name with disambiguator
suffixes, else the manual override.
"""

from __future__ import annotations

from urllib.parse import quote

from agent.enrich import http

REST_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary"
# Tried in order; first non-disambiguation hit with text wins.
DISAMBIGUATORS = ["", "_(author)", "_(writer)", "_(novelist)", "_(historian)"]

# Known bare-name mis-matches → forced correct title, or None to skip entirely.
# Salvaged from the stranded enricher so re-runs don't re-pick the wrong person.
OVERRIDES: dict[str, str | None] = {
    "paul-graham":     "Paul_Graham_(programmer)",    # YC essayist, not the novelist
    "daniel-suarez":   "Daniel_Suarez_(author)",      # tech thrillers, not the racing driver
    "robert-wright":   "Robert_Wright_(journalist)",  # Moral Animal, not the composer
    "steve-weber":     None,                          # poli-sci prof — no clean page
    "thomas-campbell": None,                          # China Study coauthor — no clean page
    "william-jordan":  None,                          # naturalist — no clean page
    "bruce-white":     None,                          # MN historian — no clean page
}


def _fetch(title: str) -> dict | None:
    return http.get_json(f"{REST_SUMMARY}/{quote(title, safe='_()')}", timeout=15)


def _usable(page: dict | None) -> dict | None:
    if not page or page.get("type") == "disambiguation":
        return None
    extract = (page.get("extract") or "").strip()
    if not extract:
        return None
    thumb = (page.get("thumbnail") or {}).get("source") or (
        page.get("originalimage") or {}).get("source")
    return {
        "extract": extract,
        "thumbnail_url": thumb,
        "wikipedia_url": (page.get("content_urls") or {}).get("desktop", {}).get("page"),
    }


def summary(slug: str, name: str, wikipedia_title: str | None = None) -> dict | None:
    """Best Wikipedia summary for an author: prefer an exact title (from Wikidata),
    honor overrides, else try the name with disambiguator suffixes."""
    if wikipedia_title:
        found = _usable(_fetch(wikipedia_title.replace(" ", "_")))
        if found:
            return found
    if slug in OVERRIDES:
        override = OVERRIDES[slug]
        return _usable(_fetch(override)) if override else None
    base = name.replace(" ", "_")
    for suffix in DISAMBIGUATORS:
        found = _usable(_fetch(base + suffix))
        if found:
            return found
    return None
