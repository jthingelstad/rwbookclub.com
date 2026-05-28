"""Backfill Open Library subject tags per book.

Each book has an olKey pointing to its OL Work record; the work's `subjects`
field is a list of free-text tags (genre, theme, period, geography). Storing
the top-N most distinctive per book gives Oliver richer thematic matching
than the 11 topic categories alone — e.g., a "set in WWII" query becomes
findable even when the topic is "History & Economics".

Self-healing: only fetches where `subjects` is missing locally, so safe to
re-run. Skips overly generic tags ("Fiction", "English language") that
don't add discrimination value at the corpus scale.

    python -m corpus.openlibrary_subjects
"""

from __future__ import annotations

import json
import time

import requests

from corpus.paths import DATA_DIR

OL = "https://openlibrary.org"
HEADERS = {"User-Agent": "rwbookclub-subjects-enricher/1.0 (https://rwbookclub.com)"}
SLEEP_BETWEEN = 0.2
MAX_SUBJECTS = 12
MAX_TAG_LEN = 80

# Generic terms that don't help narrow a 179-book corpus — we already have a
# topic taxonomy and a fiction flag for these axes.
SKIP_TAGS = {
    "fiction", "nonfiction", "non-fiction", "english language", "literature",
    "general", "audiobook", "open library staff picks", "popular print",
    "accessible book", "protected daisy", "in library", "books", "history",
}


def _clean_subjects(raw) -> list[str]:
    """Normalize, dedupe, skip generic tags, cap to MAX_SUBJECTS."""
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


def fetch_work_subjects(ol_key: str) -> list[str]:
    """Subjects on the OL Work record. Sparse for many newer/duplicate works."""
    if not ol_key:
        return []
    try:
        r = requests.get(f"{OL}{ol_key}.json", headers=HEADERS, timeout=20)
        if not r.ok:
            return []
        return _clean_subjects(r.json().get("subjects"))
    except Exception:  # noqa: BLE001
        return []


def _author_matches(doc_authors: list[str], our_authors: list[str]) -> bool:
    """Loose author-name match: any token-level overlap. Guards against picking
    a same-titled book by a different author (the "A World Appears" trap)."""
    if not our_authors:
        return True  # no constraint
    doc_lower = " ".join(a.lower() for a in doc_authors or [])
    if not doc_lower:
        return False  # we have authors, doc has none — can't match
    for ours in our_authors:
        # last-name match is usually sufficient and tolerates initials/middle names
        last = ours.lower().split()[-1] if ours else ""
        if last and last in doc_lower:
            return True
    return False


def search_best_match(title: str, authors: list[str]) -> dict | None:
    """Search OL for the right book. Requires author match (when we have one)
    before considering a doc — search is ordered by relevance so the first
    author-matching hit is the right book. Falls back through query shapes."""
    if not title:
        return None
    queries: list[dict] = []
    if authors:
        queries.append({"title": title, "author": authors[0]})
    queries.append({"title": title})
    queries.append({"q": f"{title} {authors[0] if authors else ''}".strip()})

    for params in queries:
        params.update({"limit": 10, "fields": "key,title,subject,author_name"})
        try:
            r = requests.get(f"{OL}/search.json", params=params, headers=HEADERS, timeout=20)
            if not r.ok:
                continue
            docs = r.json().get("docs") or []
        except Exception:  # noqa: BLE001
            continue
        time.sleep(SLEEP_BETWEEN)
        # Author-matching docs ranked by relevance (first wins); among those,
        # prefer ones that carry subjects.
        candidates = [d for d in docs if _author_matches(d.get("author_name") or [], authors)]
        with_subjects = [d for d in candidates if d.get("subject")]
        if with_subjects:
            return with_subjects[0]
        if candidates:
            return candidates[0]
    return None


def main() -> None:
    added_subjects = backfilled_keys = skipped = no_match = 0
    for path in sorted((DATA_DIR / "books").glob("*.json")):
        rec = json.loads(path.read_text())
        if rec.get("subjects"):
            skipped += 1
            continue
        changed = False

        # Step 1 — backfill olKey if missing, via OL search.
        if not rec.get("olKey"):
            match = search_best_match(rec.get("title"), rec.get("authors") or [])
            time.sleep(SLEEP_BETWEEN)
            if match and match.get("key"):
                rec["olKey"] = match["key"]
                backfilled_keys += 1
                changed = True
                print(f"  ↻ backfilled olKey for {rec['title']}: {match['key']}")
                # Hand the subject list off so we don't search twice.
                subjects = _clean_subjects(match.get("subject"))
                if subjects:
                    rec["subjects"] = subjects
                    added_subjects += 1
                    print(f"     + subjects: {', '.join(subjects[:4])}…")
                    path.write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
                    continue

        # Step 2 — work-level subjects.
        subjects = fetch_work_subjects(rec.get("olKey"))
        time.sleep(SLEEP_BETWEEN)

        # Step 3 — fall back to search.json subjects when the work is sparse.
        # Many books have duplicate OL Work records; the search index aggregates
        # the subject data across the richer alternates.
        if not subjects:
            match = search_best_match(rec.get("title"), rec.get("authors") or [])
            time.sleep(SLEEP_BETWEEN)
            if match:
                subjects = _clean_subjects(match.get("subject"))

        if subjects:
            rec["subjects"] = subjects
            added_subjects += 1
            preview = ", ".join(subjects[:4])
            print(f"  + {rec['title']}: {preview}…")
            changed = True
        else:
            no_match += 1
            print(f"  - {rec['title']}: no subjects (work + search both empty)")

        if changed:
            path.write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n")

    print(
        f"\nadded subjects for {added_subjects}, backfilled {backfilled_keys} olKeys, "
        f"skipped {skipped} already-present, {no_match} still no subjects"
    )


if __name__ == "__main__":
    main()
