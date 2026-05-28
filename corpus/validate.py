"""Referential integrity for the normalized corpus — the "foreign keys" check.

Since relationships are slug references in text (not enforced by a DB), this asserts
every reference resolves: meeting.books, book.picker, review.book/member,
award.books/voters, and each book author has an authors/ entry. Exits non-zero on any
dangling reference. Run locally or in CI:  python -m corpus.validate
"""

from __future__ import annotations

import json
import sys

import yaml

from pathlib import Path

from corpus.paths import DATA_DIR


def _load_dir(data_dir: Path, name: str) -> dict[str, dict]:
    return {p.stem: json.loads(p.read_text()) for p in sorted((data_dir / name).glob("*.json"))}


def validate_data_dir(data_dir: Path = DATA_DIR) -> list[str]:
    books = _load_dir(data_dir, "books")
    members = _load_dir(data_dir, "members")
    meetings = _load_dir(data_dir, "meetings")
    authors = _load_dir(data_dir, "authors")

    book_slugs = set(books)
    member_slugs = set(members)
    author_names = {a["name"] for a in authors.values()}

    errors: list[str] = []

    for slug, b in books.items():
        for p in b.get("picker") or []:
            if p not in member_slugs:
                errors.append(f"books/{slug}: picker '{p}' is not a member")
        for a in b.get("authors") or []:
            if a not in author_names:
                errors.append(f"books/{slug}: author '{a}' has no authors/ entry")

    for stem, m in meetings.items():
        for bs in m.get("books") or []:
            if bs not in book_slugs:
                errors.append(f"meetings/{stem}: book '{bs}' does not exist")

    for p in sorted((data_dir / "reviews").glob("*.md")):
        fm = yaml.safe_load(p.read_text().split("---", 2)[1]) or {}
        if fm.get("book") not in book_slugs:
            errors.append(f"reviews/{p.stem}: book '{fm.get('book')}' does not exist")
        if fm.get("member") not in member_slugs:
            errors.append(f"reviews/{p.stem}: member '{fm.get('member')}' is not a member")

    for stem, a in _load_dir(data_dir, "awards").items():
        for bs in a.get("books") or []:
            if bs not in book_slugs:
                errors.append(f"awards/{stem}: book '{bs}' does not exist")
        for v in a.get("voters") or []:
            if v not in member_slugs:
                errors.append(f"awards/{stem}: voter '{v}' is not a member")

    return errors


def main() -> int:
    books = _load_dir(DATA_DIR, "books")
    members = _load_dir(DATA_DIR, "members")
    meetings = _load_dir(DATA_DIR, "meetings")
    authors = _load_dir(DATA_DIR, "authors")
    errors = validate_data_dir(DATA_DIR)

    if errors:
        print(f"✗ {len(errors)} dangling reference(s):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"✓ corpus references OK: {len(books)} books, {len(meetings)} meetings, "
          f"{len(members)} members, {len(authors)} authors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
