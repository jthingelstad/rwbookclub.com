"""Ensure every book has cover-image variants on disk, fetching any that are
missing from Open Library (via the book's OL Key).

Git is the source of truth: book metadata lives in corpus/data/books/*.json and
the resized cover JPEGs live in website/src/assets/images/covers/. This script
is idempotent and self-healing — it only does work for books whose covers are
missing, so it can run any time (e.g. after a new book is added).

Run from the repo root:  python -m corpus.images

Member photos are no longer fetched automatically (Airtable held those URLs).
Existing photos are committed; add a new member's photo file manually.
"""

from __future__ import annotations

import json
import sys
from io import BytesIO

import requests
from PIL import Image

from corpus.airtable import COVERS_DIR, DATA_DIR

COVER_WIDTHS = [240, 480, 960]
JPEG_QUALITY = 82
BOOKS_DIR = DATA_DIR / "books"


def has_cover(slug: str) -> bool:
    return COVERS_DIR.exists() and any(COVERS_DIR.glob(f"{slug}-*.jpg"))


def ol_cover_url(ol_key: str) -> str | None:
    """Resolve an Open Library Work key (/works/OL..W) to a cover image URL."""
    try:
        r = requests.get(f"https://openlibrary.org{ol_key}.json", timeout=30)
        r.raise_for_status()
        covers = [c for c in (r.json().get("covers") or []) if isinstance(c, int) and c > 0]
        if covers:
            return f"https://covers.openlibrary.org/b/id/{covers[0]}-L.jpg"
    except Exception as e:  # noqa: BLE001 - network/parse errors are non-fatal
        print(f"    OL lookup failed for {ol_key}: {e}", file=sys.stderr)
    return None


def process_image(url: str, base_filename: str, out_dir, widths: list[int]) -> list[int]:
    """Download an image, resize to each width (capped at source), write JPEGs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    img = Image.open(BytesIO(r.content)).convert("RGB")
    src_w, src_h = img.size
    aspect = src_h / src_w
    saved: list[int] = []
    for w in widths:
        if w >= src_w:
            resized, actual_w = img, src_w
        else:
            actual_w = w
            resized = img.resize((actual_w, round(actual_w * aspect)), Image.LANCZOS)
        resized.save(
            out_dir / f"{base_filename}-{actual_w}.jpg",
            "JPEG",
            quality=JPEG_QUALITY,
            optimize=True,
            progressive=True,
        )
        if actual_w not in saved:
            saved.append(actual_w)
    return saved


def main() -> None:
    books = [json.loads(p.read_text()) for p in sorted(BOOKS_DIR.glob("*.json"))]
    missing = [b for b in books if not has_cover(b["slug"])]
    print(f"{len(books)} books; {len(missing)} missing covers")
    for b in missing:
        slug, ol = b["slug"], b.get("olKey")
        if not ol:
            print(f"  ✗ {slug}: no OL Key — add a cover manually", file=sys.stderr)
            continue
        url = ol_cover_url(ol)
        if not url:
            print(f"  ✗ {slug}: no Open Library cover", file=sys.stderr)
            continue
        try:
            widths = process_image(url, slug, COVERS_DIR, COVER_WIDTHS)
            print(f"  ✓ {slug} ({', '.join(str(w) for w in widths)})")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {slug}: {e}", file=sys.stderr)
    print("Done.")


if __name__ == "__main__":
    main()
