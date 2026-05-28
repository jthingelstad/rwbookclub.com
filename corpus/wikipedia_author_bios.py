"""Backfill missing author bios from Wikipedia (the long tail beyond Airtable).

Of the 177 authors in the corpus, 79 had bios in the Airtable cold backup; this
enricher fetches Wikipedia summaries for the rest. Self-healing — only writes
where `bio` is missing locally, so safe to re-run. Pairs with
restore_author_bios.py (Airtable backup) as a complementary source.

    python -m corpus.wikipedia_author_bios

Uses the public Wikimedia REST API (no auth). Tries the bare name first, then
`(author)` / `(writer)` / `(novelist)` disambiguators; skips disambiguation
pages and authors with no plausible match.
"""

from __future__ import annotations

import json
import time
from urllib.parse import quote

import requests

from corpus.paths import DATA_DIR

WIKI = "https://en.wikipedia.org/api/rest_v1/page/summary"
HEADERS = {"User-Agent": "rwbookclub-bio-enricher/1.0 (https://rwbookclub.com)"}
# Tried in order; first non-disambiguation hit with text wins.
DISAMBIGUATORS = ["", "_(author)", "_(writer)", "_(novelist)", "_(historian)"]
SLEEP_BETWEEN = 0.2  # be polite to Wikimedia

# Known disambiguation issues. The bare-name lookup picks the wrong person, so
# we force the correct Wikipedia title — or `None` to skip entirely if there's
# no clean page. Keeps re-runs from re-picking the wrong match.
OVERRIDES: dict[str, str | None] = {
    "paul-graham":     "Paul_Graham_(programmer)",    # YC essayist, not the novelist
    "daniel-suarez":   "Daniel_Suarez_(author)",      # tech thrillers, not the racing driver
    "robert-wright":   "Robert_Wright_(journalist)",  # Moral Animal, not Hollywood composer
    "steve-weber":     None,                          # poli-sci prof — no clean Wikipedia page
    "thomas-campbell": None,                          # China Study coauthor — no clean page
    "william-jordan":  None,                          # naturalist — no clean page
    "bruce-white":     None,                          # MN historian — no clean page
}


def _fetch(title: str) -> dict | None:
    url = f"{WIKI}/{quote(title, safe='_()')}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
    except requests.RequestException:
        return None
    return r.json() if r.status_code == 200 else None


def _bio_from_page(page: dict | None) -> str | None:
    if not page or page.get("type") == "disambiguation":
        return None
    return (page.get("extract") or "").strip() or None


def wikipedia_bio(slug: str, name: str) -> str | None:
    if slug in OVERRIDES:
        override = OVERRIDES[slug]
        return _bio_from_page(_fetch(override)) if override else None
    base = name.replace(" ", "_")
    for suffix in DISAMBIGUATORS:
        bio = _bio_from_page(_fetch(base + suffix))
        if bio:
            return bio
    return None


def main() -> None:
    added = skipped = no_match = 0
    for path in sorted((DATA_DIR / "authors").glob("*.json")):
        rec = json.loads(path.read_text())
        if rec.get("bio"):
            skipped += 1
            continue
        bio = wikipedia_bio(path.stem, rec["name"])
        time.sleep(SLEEP_BETWEEN)
        if not bio:
            no_match += 1
            print(f"  - {rec['name']}: no wikipedia match")
            continue
        rec["bio"] = bio
        path.write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
        added += 1
        snippet = bio[:80].replace("\n", " ")
        print(f"  + {rec['name']}: {snippet}…")

    print()
    print(
        f"added {added} bios from Wikipedia, skipped {skipped} already-present, "
        f"{no_match} authors with no Wikipedia match"
    )


if __name__ == "__main__":
    main()
