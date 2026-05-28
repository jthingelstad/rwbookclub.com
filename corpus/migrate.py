"""One-time migration: explode the grouped Airtable-fetch JSON into per-entity
files (Git becomes the source of truth), and pull the Meetings table from
Airtable so meetings become first-class entities.

Run once from the repo root:  python -m corpus.migrate
Reads the committed grouped JSON (books/members/authors/reviews/awards) so the
website data is reproduced exactly; hits Airtable only for Meetings (PAT in .env).

After this runs and the website is repointed, the grouped files and the
Airtable fetch pipeline are retired (Airtable kept as a cold backup).
"""

from __future__ import annotations

import json
from collections import Counter

import yaml

from corpus.paths import DATA_DIR, slugify
from corpus.airtable import (
    MEETINGS,
    airtable_session,
    list_all,
    load_env,
)

RAW = DATA_DIR / "raw"


def _write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def _load(name):
    return json.loads((DATA_DIR / name).read_text())


def _unique(slug: str, seen: set[str], fallback: str) -> str:
    """Avoid filename collisions: suffix with a stable fallback if needed."""
    s = slug or fallback
    if s in seen:
        s = f"{s}-{fallback}"
    seen.add(s)
    return s


def explode_books() -> dict[str, str]:
    books = _load("raw/books.json")
    id_to_slug = {}
    seen: set[str] = set()
    for b in books:
        slug = _unique(b["slug"], seen, str(b.get("bookId") or b["id"]))
        id_to_slug[b["id"]] = b["slug"]
        rec = {k: v for k, v in b.items() if k != "coverUrl"}  # drop ephemeral URL
        _write_json(DATA_DIR / "books" / f"{slug}.json", rec)
    print(f"  books:   {len(books)}")
    return id_to_slug


def explode_members() -> dict[str, dict]:
    members = _load("raw/members.json")
    by_id = {}
    seen: set[str] = set()
    for m in members:
        slug = _unique(m["slug"], seen, m["id"])
        by_id[m["id"]] = {"name": m["name"], "slug": m["slug"], "isCurrent": m["isCurrent"]}
        rec = {k: v for k, v in m.items() if k != "photoUrl"}  # drop ephemeral URL
        _write_json(DATA_DIR / "members" / f"{slug}.json", rec)
    print(f"  members: {len(members)}")
    return by_id


def explode_authors() -> None:
    authors = _load("authors.json")
    seen: set[str] = set()
    for a in authors:
        slug = _unique(slugify(a["name"]), seen, a["id"])
        _write_json(DATA_DIR / "authors" / f"{slug}.json", a)
    print(f"  authors: {len(authors)}")


def explode_awards() -> None:
    awards = _load("awards.json")
    seen: set[str] = set()
    for a in awards:
        base = f"{a.get('year') or 'na'}-{slugify(a.get('name') or 'award')}"
        slug = _unique(base, seen, a["id"])
        _write_json(DATA_DIR / "awards" / f"{slug}.json", a)
    print(f"  awards:  {len(awards)}")


def explode_reviews(book_slug: dict[str, str], member: dict[str, dict]) -> None:
    reviews = _load("reviews.json")
    seen: set[str] = set()
    for r in reviews:
        bslug = book_slug.get((r.get("bookIds") or [None])[0], "unknown-book")
        reviewers = r.get("reviewers") or []
        mslug = (reviewers[0].get("slug") if reviewers else None) or (
            slugify(reviewers[0]["name"]) if reviewers else "unknown-member"
        )
        name = _unique(f"{bslug}--{mslug}", seen, r["id"])
        # Frontmatter preserves the exact record fields used by the site's joins;
        # human-friendly book/member slugs are added alongside. Body = prose.
        front = {
            "id": r["id"],
            "book": bslug,
            "member": mslug,
            "bookIds": r.get("bookIds") or [],
            "memberIds": r.get("memberIds") or [],
            "reviewers": reviewers,
            "rating": r.get("rating"),
            "dnf": bool(r.get("dnf")),
            "discussionQuality": r.get("discussionQuality"),
            "wouldRecommend": bool(r.get("wouldRecommend")),
            "favoriteQuote": r.get("favoriteQuote"),
            "createdAt": r.get("createdAt"),
        }
        fm = yaml.safe_dump(front, sort_keys=False, allow_unicode=True, default_flow_style=False)
        body = (r.get("review") or "").strip()
        path = DATA_DIR / "reviews" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\n{fm}---\n\n{body}\n" if body else f"---\n{fm}---\n")
    print(f"  reviews: {len(reviews)}")


def import_meetings(book_slug: dict[str, str], member: dict[str, dict]) -> None:
    base, pat = load_env()
    session = airtable_session(pat)
    rows = list_all(session, base, MEETINGS)
    seen: set[str] = set()
    for m in rows:
        f = m["fields"]
        date = f.get("Meeting Date")
        book_ids = f.get("Book") or []
        host_ids = f.get("Host") or []
        rec = {
            "id": m["id"],
            "meetingId": f.get("Meeting ID"),
            "name": f.get("Name"),
            "date": date,
            "year": int(date[:4]) if date else None,
            "bookIds": book_ids,
            "bookSlugs": [book_slug[b] for b in book_ids if b in book_slug],
            "hostIds": host_ids,
            "hosts": [
                {"name": member[h]["name"], "slug": member[h]["slug"]}
                for h in host_ids
                if h in member
            ],
            "type": f.get("Meeting Type") or [],
            "location": (f.get("Location") or "").strip() or None,
            "notes": (f.get("Notes") or "").strip() or None,
            "placeholder": bool(f.get("Placeholder")),
        }
        day = (date or "")[:10] or "undated"
        base_name = _unique(f"{day}--{f.get('Meeting ID') or m['id']}", seen, m["id"])
        _write_json(DATA_DIR / "meetings" / f"{base_name}.json", rec)
    print(f"  meetings:{len(rows)}  (from Airtable)")


def main() -> None:
    print("Exploding committed JSON into per-entity files…")
    id_to_slug = explode_books()
    member = explode_members()
    explode_authors()
    explode_awards()
    explode_reviews(id_to_slug, member)
    import_meetings(id_to_slug, member)
    print("Done. Remove the grouped JSON + raw/ once the website is repointed.")


if __name__ == "__main__":
    main()
