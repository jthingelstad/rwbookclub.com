"""Download book covers and member photos referenced in src/_data/*.json,
resize them with Pillow, and write WebP + JPEG fallbacks at multiple widths.

Strips the ephemeral signed URLs from the JSON afterward so the templates
only see local filenames.
"""

from __future__ import annotations

import json
import sys
from io import BytesIO

import requests
from PIL import Image

from lib import COVERS_DIR, DATA_DIR, MEMBERS_IMG_DIR

COVER_WIDTHS = [240, 480, 960]
PHOTO_WIDTHS = [240, 480]
JPEG_QUALITY = 82
WEBP_QUALITY = 80


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
        webp_path = out_dir / f"{base_filename}-{actual_w}.webp"
        jpeg_path = out_dir / f"{base_filename}-{actual_w}.jpg"
        resized.save(webp_path, "WEBP", quality=WEBP_QUALITY, method=6)
        resized.save(
            jpeg_path, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True
        )
        if actual_w not in saved_widths:
            saved_widths.append(actual_w)
    return saved_widths


def main() -> None:
    books = json.loads((DATA_DIR / "books.json").read_text())
    members = json.loads((DATA_DIR / "members.json").read_text())

    cover_count = sum(1 for b in books if b.get("coverUrl"))
    print(f"Processing {cover_count} book covers → {COVERS_DIR.relative_to(COVERS_DIR.parent.parent.parent)}")
    for b in books:
        url = b.get("coverUrl")
        if not url:
            continue
        try:
            widths = process_image(url, b["slug"], COVERS_DIR, COVER_WIDTHS)
            b["coverWidths"] = widths
            print(f"  ✓ {b['slug']} ({', '.join(str(w) for w in widths)})")
        except Exception as e:
            print(f"  ✗ {b['slug']}: {e}", file=sys.stderr)
            b["hasCover"] = False

    photo_count = sum(1 for m in members if m.get("photoUrl"))
    print(f"Processing {photo_count} member photos")
    for m in members:
        url = m.get("photoUrl")
        if not url:
            continue
        try:
            widths = process_image(url, m["slug"], MEMBERS_IMG_DIR, PHOTO_WIDTHS)
            m["photoWidths"] = widths
            print(f"  ✓ {m['slug']} ({', '.join(str(w) for w in widths)})")
        except Exception as e:
            print(f"  ✗ {m['slug']}: {e}", file=sys.stderr)
            m["hasPhoto"] = False

    # Strip ephemeral signed URLs before 11ty consumes the JSON
    for b in books:
        b.pop("coverUrl", None)
    for m in members:
        m.pop("photoUrl", None)

    (DATA_DIR / "books.json").write_text(
        json.dumps(books, indent=2, ensure_ascii=False)
    )
    (DATA_DIR / "members.json").write_text(
        json.dumps(members, indent=2, ensure_ascii=False)
    )
    print("Done.")


if __name__ == "__main__":
    main()
