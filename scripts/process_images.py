"""Download book covers and member photos referenced in
src/_data/raw/*.json, resize them with Pillow, and write progressive JPEGs
at multiple widths.

The 11ty data layer (src/_data/books.js, src/_data/members.js) derives
`coverWidths`/`photoWidths`/`hasCover`/`hasPhoto` from whatever JPEGs
exist on disk at build time, so this script no longer needs to write
anything back into the JSON. Its only job is downloading and resizing.
"""

from __future__ import annotations

import json
import sys
from io import BytesIO

import requests
from PIL import Image

from lib import COVERS_DIR, MEMBERS_IMG_DIR, RAW_DATA_DIR

COVER_WIDTHS = [240, 480, 960]
PHOTO_WIDTHS = [240, 480]
JPEG_QUALITY = 82


def process_image(url: str, base_filename: str, out_dir, widths: list[int]) -> list[int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    img = Image.open(BytesIO(r.content)).convert("RGB")
    src_w, src_h = img.size
    aspect = src_h / src_w
    saved_widths: list[int] = []
    for w in widths:
        if w >= src_w:
            resized = img
            actual_w = src_w
        else:
            actual_w = w
            actual_h = round(w * aspect)
            resized = img.resize((actual_w, actual_h), Image.LANCZOS)
        jpeg_path = out_dir / f"{base_filename}-{actual_w}.jpg"
        resized.save(
            jpeg_path, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True
        )
        if actual_w not in saved_widths:
            saved_widths.append(actual_w)
    return saved_widths


def main() -> None:
    books = json.loads((RAW_DATA_DIR / "books.json").read_text())
    members = json.loads((RAW_DATA_DIR / "members.json").read_text())

    cover_count = sum(1 for b in books if b.get("coverUrl"))
    print(f"Processing {cover_count} book covers")
    for b in books:
        url = b.get("coverUrl")
        if not url:
            continue
        try:
            widths = process_image(url, b["slug"], COVERS_DIR, COVER_WIDTHS)
            print(f"  ✓ {b['slug']} ({', '.join(str(w) for w in widths)})")
        except Exception as e:
            print(f"  ✗ {b['slug']}: {e}", file=sys.stderr)

    photo_count = sum(1 for m in members if m.get("photoUrl"))
    print(f"Processing {photo_count} member photos")
    for m in members:
        url = m.get("photoUrl")
        if not url:
            continue
        try:
            widths = process_image(url, m["slug"], MEMBERS_IMG_DIR, PHOTO_WIDTHS)
            print(f"  ✓ {m['slug']} ({', '.join(str(w) for w in widths)})")
        except Exception as e:
            print(f"  ✗ {m['slug']}: {e}", file=sys.stderr)

    print("Done.")


if __name__ == "__main__":
    main()
