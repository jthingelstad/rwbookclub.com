"""Re-pull author Bios from the Airtable cold backup into the per-entity files.

Background: the original `fetch.py` author projection didn't capture `Bio`, so when
the corpus was normalized to per-entity files every author lost its bio. This
enricher patches `corpus/data/authors/<slug>.json` in place from the Airtable
backup. Self-healing — only writes when bio is missing locally AND present in
Airtable, so safe to re-run.

    python -m corpus.restore_author_bios
"""

from __future__ import annotations

import json

from corpus.airtable import (
    AUTHORS,
    DATA_DIR,
    airtable_session,
    list_all,
    load_env,
    slugify,
)


def main() -> None:
    base, pat = load_env()
    session = airtable_session(pat)
    rows = list_all(session, base, AUTHORS)

    bios_by_slug: dict[str, str] = {}
    for r in rows:
        name = (r["fields"].get("Author") or "").strip()
        bio = (r["fields"].get("Bio") or "").strip()
        if name and bio:
            bios_by_slug[slugify(name)] = bio

    added = skipped = no_bio = 0
    for path in sorted((DATA_DIR / "authors").glob("*.json")):
        rec = json.loads(path.read_text())
        if rec.get("bio"):  # already present locally — leave it alone
            skipped += 1
            continue
        bio = bios_by_slug.get(path.stem)
        if not bio:
            no_bio += 1
            continue
        rec["bio"] = bio
        path.write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
        added += 1

    print(
        f"restored {added} bios, skipped {skipped} already-present, "
        f"{no_bio} authors with no Airtable bio (Airtable rows: {len(rows)})"
    )


if __name__ == "__main__":
    main()
