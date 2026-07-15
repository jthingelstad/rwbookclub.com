"""Schema and referential-integrity validation for the generated corpus contract.

The corpus is a versioned cross-language API. This module checks each document's required fields
and types first, then checks every slug/name relationship that SQLite cannot enforce once the data
has been projected to files. Run locally or in CI with ``python -m corpus.validate``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from corpus.paths import DATA_DIR
from corpus.schema import Manifest, validation_errors


def _load_json_dir(data_dir: Path, name: str, errors: list[str]) -> dict[str, dict]:
    documents: dict[str, dict] = {}
    for path in sorted((data_dir / name).glob("*.json")):
        label = f"{name}/{path.stem}"
        try:
            document = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{label}: invalid JSON: {exc}")
            continue
        if not isinstance(document, dict):
            errors.append(f"{label}: document must be a JSON object")
            continue
        documents[path.stem] = document
        errors.extend(f"{label}: {message}" for message in validation_errors(name, document))
    return documents


def _load_reviews(data_dir: Path, errors: list[str]) -> dict[str, dict]:
    reviews: dict[str, dict] = {}
    for path in sorted((data_dir / "reviews").glob("*.md")):
        label = f"reviews/{path.stem}"
        text = path.read_text()
        if not text.startswith("---") or len(text.split("---", 2)) < 3:
            errors.append(f"{label}: missing YAML frontmatter")
            continue
        try:
            document = yaml.safe_load(text.split("---", 2)[1]) or {}
        except yaml.YAMLError as exc:
            errors.append(f"{label}: invalid YAML: {exc}")
            continue
        if not isinstance(document, dict):
            errors.append(f"{label}: frontmatter must be an object")
            continue
        reviews[path.stem] = document
        errors.extend(f"{label}: {message}" for message in validation_errors("reviews", document))
    return reviews


def _validate_manifest(data_dir: Path, errors: list[str]) -> None:
    path = data_dir / "manifest.json"
    if not path.exists():
        errors.append("manifest: missing manifest.json")
        return
    try:
        Manifest.model_validate_json(path.read_text())
    except (OSError, ValueError) as exc:
        errors.append(f"manifest: {exc}")


def validate_data_dir(data_dir: Path = DATA_DIR) -> list[str]:
    errors: list[str] = []
    _validate_manifest(data_dir, errors)
    books = _load_json_dir(data_dir, "books", errors)
    members = _load_json_dir(data_dir, "members", errors)
    meetings = _load_json_dir(data_dir, "meetings", errors)
    authors = _load_json_dir(data_dir, "authors", errors)
    lists = _load_json_dir(data_dir, "lists", errors)
    reviews = _load_reviews(data_dir, errors)

    book_slugs = set(books)
    member_slugs = set(members)
    author_names = {author.get("name") for author in authors.values()}

    for slug, book in books.items():
        for picker in book.get("picker") or []:
            if picker not in member_slugs:
                errors.append(f"books/{slug}: picker '{picker}' is not a member")
        for author in book.get("authors") or []:
            if author not in author_names:
                errors.append(f"books/{slug}: author '{author}' has no authors/ entry")

    for stem, meeting in meetings.items():
        for book_slug in meeting.get("books") or []:
            if book_slug not in book_slugs:
                errors.append(f"meetings/{stem}: book '{book_slug}' does not exist")
        for host in meeting.get("host") or []:
            if host not in member_slugs:
                errors.append(f"meetings/{stem}: host '{host}' is not a member")

    for stem, review in reviews.items():
        if review.get("book") not in book_slugs:
            errors.append(f"reviews/{stem}: book '{review.get('book')}' does not exist")
        if review.get("member") not in member_slugs:
            errors.append(f"reviews/{stem}: member '{review.get('member')}' is not a member")

    for stem, book_list in lists.items():
        for entry in book_list.get("books") or []:
            book_slug = entry.get("book") if isinstance(entry, dict) else entry
            if book_slug not in book_slugs:
                errors.append(f"lists/{stem}: book '{book_slug}' does not exist")
        owner = book_list.get("owner")
        if owner is not None and owner not in member_slugs:
            errors.append(f"lists/{stem}: owner '{owner}' is not a member")

    return errors


def main() -> int:
    errors = validate_data_dir(DATA_DIR)
    if errors:
        print(f"✗ {len(errors)} corpus contract violation(s):", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    counts = {
        name: len(list((DATA_DIR / name).glob(pattern)))
        for name, pattern in (
            ("books", "*.json"),
            ("meetings", "*.json"),
            ("members", "*.json"),
            ("authors", "*.json"),
        )
    }
    print(
        "✓ corpus v1 contract OK: "
        f"{counts['books']} books, {counts['meetings']} meetings, "
        f"{counts['members']} members, {counts['authors']} authors"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
