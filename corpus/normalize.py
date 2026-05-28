"""One-time: normalize the per-entity corpus — strip denormalized/derived fields.

Books keep intrinsic fields + `picker:[member-slug]`; meetings own `date` + `books:[slug]`;
members/authors keep identity only; reviews and awards use slug references. Everything else
(meeting date on a book, a member's picks, review counts, names behind slugs) is derived at
build/read time. Idempotent-ish: reads the current per-entity files and rewrites them in the
normalized shape. Run once from the repo root:

    python -m corpus.normalize
"""

from __future__ import annotations

import json

import yaml

from corpus.paths import DATA_DIR

# slug is the filename, and the Airtable rec id is no longer referenced — both omitted.
BOOK_KEEP = [
    "bookId", "title", "subtitle", "authors", "topic", "fiction",
    "publicationYear", "pageCount", "isbn13", "olKey", "synopsis",
    "subjects",  # OL subject tags backfilled via corpus.openlibrary_subjects
]
MEMBER_KEEP = ["name", "isCurrent", "website"]


def _load_dir(name: str) -> dict[str, dict]:
    d = DATA_DIR / name
    return {p.stem: json.loads(p.read_text()) for p in sorted(d.glob("*.json"))}


def _write(path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def _split_md(text: str) -> tuple[dict, str]:
    fm, body = text.split("---", 2)[1], text.split("---", 2)[2]
    return yaml.safe_load(fm) or {}, body.strip()


def main() -> None:
    books = _load_dir("books")
    members = _load_dir("members")
    meetings = _load_dir("meetings")
    authors = _load_dir("authors")

    book_slug_by_id = {b["id"]: b["slug"] for b in books.values()}
    member_slug_by_name = {m["name"]: m["slug"] for m in members.values()}

    # Books: intrinsic + picker (member slugs, resolved from the old pickerNames).
    for slug, b in books.items():
        rec = {k: b.get(k) for k in BOOK_KEEP}
        rec["picker"] = [
            member_slug_by_name[n] for n in (b.get("pickerNames") or [])
            if n in member_slug_by_name
        ]
        _write(DATA_DIR / "books" / f"{slug}.json", rec)

    # Meetings: own date + book refs (re-resolved from rec ids to current slugs).
    for stem, m in meetings.items():
        _write(DATA_DIR / "meetings" / f"{stem}.json", {
            "meetingId": m.get("meetingId"),
            "date": m.get("date"),
            "books": [book_slug_by_id[bid] for bid in (m.get("bookIds") or []) if bid in book_slug_by_id],
            "type": m.get("type") or [],
            "location": m.get("location"),
            "notes": m.get("notes"),
            "placeholder": bool(m.get("placeholder")),
        })

    # Members + authors: identity only.
    for slug, m in members.items():
        _write(DATA_DIR / "members" / f"{slug}.json", {k: m.get(k) for k in MEMBER_KEEP})
    for slug, a in authors.items():
        rec = {"name": a["name"]}
        if a.get("bio"):  # preserve Bio when the cold-backup carried it
            rec["bio"] = a["bio"]
        _write(DATA_DIR / "authors" / f"{slug}.json", rec)

    # Reviews: slug-based frontmatter; drop id arrays + reviewer-name copies.
    for p in sorted((DATA_DIR / "reviews").glob("*.md")):
        d, body = _split_md(p.read_text())
        front = {
            "id": d.get("id"), "book": d.get("book"), "member": d.get("member"),
            "rating": d.get("rating"), "dnf": bool(d.get("dnf")),
            "discussionQuality": d.get("discussionQuality"),
            "wouldRecommend": bool(d.get("wouldRecommend")),
            "favoriteQuote": d.get("favoriteQuote"), "createdAt": d.get("createdAt"),
        }
        fm = yaml.safe_dump(front, sort_keys=False, allow_unicode=True, default_flow_style=False)
        p.write_text(f"---\n{fm}---\n\n{body}\n" if body else f"---\n{fm}---\n")

    # Awards: slug references.
    for p in sorted((DATA_DIR / "awards").glob("*.json")):
        a = json.loads(p.read_text())
        _write(p, {
            "name": a.get("name"), "year": a.get("year"),
            "award": a.get("award"), "notes": a.get("notes"),
            "books": [bk.get("slug") for bk in (a.get("books") or []) if bk.get("slug")],
            "voters": [v.get("slug") for v in (a.get("voters") or []) if v.get("slug")],
        })

    print(f"normalized: {len(books)} books, {len(meetings)} meetings, "
          f"{len(members)} members, {len(authors)} authors")


if __name__ == "__main__":
    main()
