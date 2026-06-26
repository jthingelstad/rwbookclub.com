"""Ensure every book has cover-image variants on disk, fetching any that are
missing from Open Library.

The DB (``club_books`` + ``club_book_enrichment``) is the source of truth for a
book's OL identifiers: the enrichment loop stores ``ol_cover_id`` (preferred) and
the Work ``ol_key``. The resized cover JPEGs live in
``website/src/assets/images/covers/``. This script is idempotent and self-healing
— it only fetches covers that are missing on disk, so it can run any time.

The enrichment loop (``agent.enrich``) also fetches covers inline; this remains a
standalone backfill (``npm run covers``) for covers that slipped through.

Run from the repo root:  python -m corpus.images

Member photos are added manually; author portraits are fetched by ``agent.enrich``.
"""

from __future__ import annotations

import sys
from io import BytesIO

import requests
from PIL import Image

from corpus.paths import COVERS_DIR

COVER_WIDTHS = [240, 480, 960]
JPEG_QUALITY = 82

# A real User-Agent is required by Wikimedia Commons (author portraits) and is
# polite for Open Library; the bare requests default gets a 403 from Commons.
HEADERS = {"User-Agent": "rwbookclub-images/1.0 (https://rwbookclub.com)"}


def has_cover(slug: str) -> bool:
    return COVERS_DIR.exists() and any(COVERS_DIR.glob(f"{slug}-*.jpg"))


def cover_url_from_id(cover_id: int) -> str:
    return f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"


def ol_cover_url(ol_key: str) -> str | None:
    """Resolve an Open Library Work key (/works/OL..W) to a cover image URL."""
    try:
        r = requests.get(f"https://openlibrary.org{ol_key}.json", headers=HEADERS, timeout=30)
        r.raise_for_status()
        covers = [c for c in (r.json().get("covers") or []) if isinstance(c, int) and c > 0]
        if covers:
            return cover_url_from_id(covers[0])
    except Exception as e:  # noqa: BLE001 - network/parse errors are non-fatal
        print(f"    OL lookup failed for {ol_key}: {e}", file=sys.stderr)
    return None


def process_image(url: str, base_filename: str, out_dir, widths: list[int]) -> list[int]:
    """Download an image, resize to each width (capped at source), write JPEGs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    r = requests.get(url, headers=HEADERS, timeout=60)
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
    # Read OL identifiers from the authoritative DB (ol_cover_id preferred, else the
    # Work key) rather than the generated corpus files.
    from agent import clubdb, db

    with db.connect() as conn:
        books = [
            {"slug": b["slug"], "ol_key": b.get("ol_key"), "ol_cover_id": b.get("ol_cover_id")}
            for b in clubdb.all_books(conn)
        ]
    missing = [b for b in books if not has_cover(b["slug"])]
    print(f"{len(books)} books; {len(missing)} missing covers")
    for b in missing:
        slug = b["slug"]
        url = cover_url_from_id(b["ol_cover_id"]) if b.get("ol_cover_id") else (
            ol_cover_url(b["ol_key"]) if b.get("ol_key") else None)
        if not url:
            print(f"  ✗ {slug}: no OL cover id / key — run `python -m agent.enrich --books` "
                  f"or add a cover manually", file=sys.stderr)
            continue
        try:
            widths = process_image(url, slug, COVERS_DIR, COVER_WIDTHS)
            print(f"  ✓ {slug} ({', '.join(str(w) for w in widths)})")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {slug}: {e}", file=sys.stderr)
    print("Done.")


if __name__ == "__main__":
    main()
