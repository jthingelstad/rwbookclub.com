"""Fetch every table from the R/W Book Club Airtable base and write
denormalized JSON files for 11ty to consume.

Run after `pip install -r requirements.txt`. Reads credentials from .env.
"""

from __future__ import annotations

import json
from collections import Counter

from lib import (
    AUTHORS,
    AWARDS,
    BOOKS,
    DATA_DIR,
    MEETINGS,
    MEMBERS,
    REVIEWS,
    airtable_session,
    first_attachment_url,
    list_all,
    load_env,
    slugify,
)


def main() -> None:
    base, pat = load_env()
    session = airtable_session(pat)

    print("Fetching Airtable…")
    books_raw = list_all(session, base, BOOKS)
    meetings_raw = list_all(session, base, MEETINGS)
    members_raw = list_all(session, base, MEMBERS)
    authors_raw = list_all(session, base, AUTHORS)
    reviews_raw = list_all(session, base, REVIEWS)
    awards_raw = list_all(session, base, AWARDS)
    print(
        f"  books={len(books_raw)} meetings={len(meetings_raw)} "
        f"members={len(members_raw)} authors={len(authors_raw)} "
        f"reviews={len(reviews_raw)} awards={len(awards_raw)}"
    )

    meetings_by_id = {m["id"]: m for m in meetings_raw}
    members_by_id = {m["id"]: m for m in members_raw}
    authors_by_id = {a["id"]: a for a in authors_raw}

    # Pre-compute member metadata used during book join
    member_slug_by_id: dict[str, str] = {}
    member_current_by_id: dict[str, bool] = {}
    member_name_by_id: dict[str, str] = {}
    for m in members_raw:
        f = m["fields"]
        name = (f.get("Name") or "").strip()
        member_slug_by_id[m["id"]] = slugify(name)
        member_current_by_id[m["id"]] = bool(f.get("Current Member"))
        member_name_by_id[m["id"]] = name

    # Detect title slug collisions so we can suffix them with Book ID
    title_slug_counts: Counter[str] = Counter()
    for b in books_raw:
        title_slug_counts[slugify(b["fields"].get("Book", ""))] += 1

    # Index reviews by book record id
    reviews_by_book: dict[str, list[str]] = {}
    for r in reviews_raw:
        for bid in r["fields"].get("Book") or []:
            reviews_by_book.setdefault(bid, []).append(r["id"])

    # ── Books ────────────────────────────────────────────────────────────
    book_records: list[dict] = []
    for b in books_raw:
        f = b["fields"]
        title = (f.get("Book") or "").strip()
        subtitle = (f.get("Subtitle") or "").strip() or None

        slug_base = slugify(title)
        slug = slug_base
        if title_slug_counts[slug_base] > 1 and f.get("Book ID") is not None:
            slug = f"{slug_base}-{f.get('Book ID')}"
        if not slug:
            slug = f"book-{f.get('Book ID', b['id'])}"

        author_names = [
            authors_by_id[aid]["fields"].get("Author", "").strip()
            for aid in (f.get("Authors") or [])
            if aid in authors_by_id
        ]
        author_names = [a for a in author_names if a]

        # Resolve meetings → use earliest as the primary "date read"
        meeting_date: str | None = None
        picker_name: str | None = None
        picker_slug: str | None = None
        meeting_notes: str | None = None
        meeting_location: str | None = None
        for mid in f.get("Meetings") or []:
            m = meetings_by_id.get(mid)
            if not m:
                continue
            md = m["fields"].get("Meeting Date")
            if md and (meeting_date is None or md < meeting_date):
                meeting_date = md
                hosts = m["fields"].get("Host") or []
                if hosts:
                    host_id = hosts[0]
                    picker_name = member_name_by_id.get(host_id) or None
                    if member_current_by_id.get(host_id):
                        picker_slug = member_slug_by_id.get(host_id)
                    else:
                        picker_slug = None
                meeting_notes = (m["fields"].get("Notes") or "").strip() or None
                meeting_location = (m["fields"].get("Location") or "").strip() or None

        cover_url = first_attachment_url(f.get("Cover"))

        book_records.append(
            {
                "id": b["id"],
                "bookId": f.get("Book ID"),
                "title": title,
                "subtitle": subtitle,
                "slug": slug,
                "authors": author_names,
                "topic": f.get("Topic"),
                "fiction": bool(f.get("Fiction")),
                "publicationYear": f.get("Publication Year"),
                "pageCount": f.get("Page Count"),
                "isbn13": (f.get("ISBN-13") or "").strip() or None,
                "olKey": (f.get("OL Key") or "").strip() or None,
                "synopsis": (f.get("Synopsis") or "").strip() or None,
                "meetingDate": meeting_date,
                "year": int(meeting_date[:4]) if meeting_date else None,
                "pickerName": picker_name,
                "pickerSlug": picker_slug,
                "meetingNotes": meeting_notes,
                "meetingLocation": meeting_location,
                "coverUrl": cover_url,  # ephemeral, consumed by process_images
                "hasCover": cover_url is not None,
                "reviewCount": len(reviews_by_book.get(b["id"], [])),
            }
        )

    # Sort books most-recent first for the reading journey
    book_records.sort(
        key=lambda r: (r.get("meetingDate") or "", r.get("bookId") or 0),
        reverse=True,
    )

    slug_by_book_id = {r["id"]: r["slug"] for r in book_records}
    title_by_book_id = {r["id"]: r["title"] for r in book_records}

    # ── Members ──────────────────────────────────────────────────────────
    member_records: list[dict] = []
    for m in members_raw:
        f = m["fields"]
        name = (f.get("Name") or "").strip()
        is_current = bool(f.get("Current Member"))
        photo_url = first_attachment_url(f.get("Photo"))

        # Find books this member picked (only meaningful for current members,
        # since past members don't get pages)
        picked_books: list[dict] = []
        if is_current:
            for meeting in meetings_raw:
                if m["id"] not in (meeting["fields"].get("Host") or []):
                    continue
                md = meeting["fields"].get("Meeting Date")
                for bid in meeting["fields"].get("Book") or []:
                    if bid in slug_by_book_id:
                        picked_books.append(
                            {
                                "slug": slug_by_book_id[bid],
                                "title": title_by_book_id[bid],
                                "year": int(md[:4]) if md else None,
                                "date": md,
                            }
                        )
            picked_books.sort(key=lambda x: x.get("date") or "", reverse=True)

        member_records.append(
            {
                "id": m["id"],
                "name": name,
                "slug": slugify(name),
                "isCurrent": is_current,
                "website": ((f.get("Website") or "").strip() or None) if is_current else None,
                "photoUrl": photo_url if is_current else None,  # ephemeral
                "hasPhoto": (photo_url is not None) and is_current,
                "pickedCount": f.get("Picked Count") or 0,
                "pickedBooks": picked_books,
            }
        )

    # ── Authors (kept simple; not paginated as pages) ────────────────────
    author_records = [
        {
            "id": a["id"],
            "name": (a["fields"].get("Author") or "").strip(),
            "bookCount": a["fields"].get("Book Count") or 0,
        }
        for a in authors_raw
    ]

    # ── Reviews ──────────────────────────────────────────────────────────
    review_records = []
    for r in reviews_raw:
        f = r["fields"]
        member_ids = f.get("Member") or []
        reviewers = [
            {
                "id": mid,
                "name": member_name_by_id.get(mid),
                "slug": member_slug_by_id.get(mid)
                if member_current_by_id.get(mid)
                else None,
            }
            for mid in member_ids
            if member_name_by_id.get(mid)
        ]
        review_records.append(
            {
                "id": r["id"],
                "bookIds": f.get("Book") or [],
                "memberIds": member_ids,
                "reviewers": reviewers,
                "rating": f.get("Rating"),
                "review": (f.get("Review") or "").strip() or None,
                "dnf": bool(f.get("DNF")),
                "discussionQuality": f.get("Discussion Quality"),
                "wouldRecommend": bool(f.get("Would Recommend")),
                "favoriteQuote": (f.get("Favorite Quote") or "").strip() or None,
                "createdAt": f.get("Created at"),
            }
        )

    # ── Awards ───────────────────────────────────────────────────────────
    award_records = []
    for a in awards_raw:
        f = a["fields"]
        book_ids = f.get("Book") or []
        books = [
            {
                "id": bid,
                "slug": slug_by_book_id.get(bid),
                "title": title_by_book_id.get(bid),
            }
            for bid in book_ids
            if bid in slug_by_book_id
        ]
        voter_ids = f.get("Voted By") or []
        voters = [
            {
                "name": member_name_by_id.get(mid),
                "slug": member_slug_by_id.get(mid)
                if member_current_by_id.get(mid)
                else None,
            }
            for mid in voter_ids
            if member_name_by_id.get(mid)
        ]
        award_records.append(
            {
                "id": a["id"],
                "name": (f.get("Award Name") or "").strip() or None,
                "year": f.get("Year"),
                "award": f.get("Award"),
                "notes": (f.get("Notes") or "").strip() or None,
                "books": books,
                "voters": voters,
            }
        )

    # Sort awards: most recent year first, then by category
    _award_order = {
        "Book of the Year": 0,
        "Runner-up": 1,
        "Honorable Mention": 2,
        "Most Discussed": 3,
        "Most Surprising": 4,
        "Worst Book": 5,
    }
    award_records.sort(
        key=lambda r: (
            -(r.get("year") or 0),
            _award_order.get(r.get("award") or "", 99),
        )
    )

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "books.json").write_text(
        json.dumps(book_records, indent=2, ensure_ascii=False)
    )
    (DATA_DIR / "members.json").write_text(
        json.dumps(member_records, indent=2, ensure_ascii=False)
    )
    (DATA_DIR / "authors.json").write_text(
        json.dumps(author_records, indent=2, ensure_ascii=False)
    )
    (DATA_DIR / "reviews.json").write_text(
        json.dumps(review_records, indent=2, ensure_ascii=False)
    )
    (DATA_DIR / "awards.json").write_text(
        json.dumps(award_records, indent=2, ensure_ascii=False)
    )
    print(
        f"Wrote {len(book_records)} books, {len(member_records)} members, "
        f"{len(author_records)} authors, {len(review_records)} reviews, "
        f"{len(award_records)} awards"
    )


if __name__ == "__main__":
    main()
