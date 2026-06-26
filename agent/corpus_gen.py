"""Generate the Git corpus (``corpus/data/*``) from the authoritative ``club_*`` tables.

This is the inverse of ``corpus/normalize.py``: it reproduces the exact normalized
on-disk shape the website (`website/src/_data/*.js`) and Oliver (`agent/corpus_read.py`)
consume — same keys, same order, same serialization — so the regenerated corpus is a
faithful, diff-clean artifact of the database.

Serialization rules (must match ``corpus/normalize.py``):
  * JSON: ``json.dumps(obj, indent=2, ensure_ascii=False) + "\\n"`` (trailing newline).
  * Review markdown: ``yaml.safe_dump(front, sort_keys=False, allow_unicode=True,
    default_flow_style=False)``, body appended only when present.

Run:
    python -m agent.corpus_gen                 # write into corpus/data/
    python -m agent.corpus_gen --out /tmp/x    # write into a scratch dir (for diffing)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent import clubdb, db  # noqa: E402
from corpus.paths import slugify  # noqa: E402

DEFAULT_OUT = REPO_ROOT / "corpus" / "data"
ENTITY_DIRS = ["books", "meetings", "members", "authors", "awards", "reviews"]


def _write_json(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def _book_doc(b: dict) -> dict:
    doc = {
        "bookId": b["id"],
        "title": b["title"],
        "subtitle": b["subtitle"],
        "authors": b["author_names"],
        "topic": b["topic"],
        "fiction": bool(b["fiction"]),
        "publicationYear": b["publication_year"],
        "pageCount": b["page_count"],
        "isbn13": b["isbn13"],
        "olKey": b["ol_key"],
        "synopsis": b["synopsis"],
        "picker": b["picker_slugs"],
    }
    if b["subjects_json"] is not None:   # omit the key entirely when absent (corpus quirk)
        doc["subjects"] = b["subjects"]
    return doc


def _meeting_doc(m: dict) -> dict:
    return {
        "meetingId": m["id"],
        "date": m["date"],
        "books": m["book_slugs"],
        "type": m["type"],
        "location": m["location"],
        "notes": m["notes"],
        "placeholder": bool(m["placeholder"]),
    }


def _member_doc(m: dict) -> dict:
    return {"name": m["name"], "isCurrent": bool(m["is_current"]), "website": m["website"]}


def _author_doc(a: dict) -> dict:
    doc = {"name": a["name"]}
    if a["bio"]:                          # bio omitted (not null) when empty
        doc["bio"] = a["bio"]
    return doc


def _award_doc(a: dict) -> dict:
    return {
        "name": a["name"],
        "year": a["year"],
        "award": a["award_category"],
        "notes": a["notes"],
        "books": a["book_slugs"],
        "voters": a["voter_slugs"],
    }


def _review_text(r: dict) -> str:
    front = {
        "id": r["airtable_id"],
        "book": r["book_slug"],
        "member": r["member_slug"],
        "rating": r["rating"],
        "dnf": bool(r["dnf"]),
        "discussionQuality": r["discussion_quality"],
        "wouldRecommend": bool(r["would_recommend"]),
        "favoriteQuote": r["favorite_quote"],
        "createdAt": r["created_at"],
    }
    fm = yaml.safe_dump(front, sort_keys=False, allow_unicode=True, default_flow_style=False)
    body = (r["body"] or "").strip()
    return f"---\n{fm}---\n\n{body}\n" if body else f"---\n{fm}---\n"


def _prune(directory: Path, keep: set[str]) -> int:
    """Delete files in `directory` not in `keep` (so the dir exactly mirrors the DB)."""
    removed = 0
    for p in directory.iterdir():
        if p.is_file() and p.name not in keep:
            p.unlink()
            removed += 1
    return removed


def generate(out_root: Path = DEFAULT_OUT) -> dict:
    out_root = Path(out_root)
    for d in ENTITY_DIRS:
        (out_root / d).mkdir(parents=True, exist_ok=True)

    written = {d: 0 for d in ENTITY_DIRS}
    keep: dict[str, set[str]] = {d: set() for d in ENTITY_DIRS}

    def emit_json(kind: str, name: str, doc: dict) -> None:
        _write_json(out_root / kind / name, doc)
        keep[kind].add(name)
        written[kind] += 1

    with db.connect() as conn:
        for b in clubdb.all_books(conn):
            emit_json("books", f"{b['slug']}.json", _book_doc(b))
        for m in clubdb.all_meetings(conn):
            stem = f"{(m['date'] or 'undated')[:10]}--{m['id']}"
            emit_json("meetings", f"{stem}.json", _meeting_doc(m))
        for m in clubdb.all_members(conn):
            emit_json("members", f"{m['slug']}.json", _member_doc(m))
        for a in clubdb.all_authors(conn):
            emit_json("authors", f"{a['slug']}.json", _author_doc(a))
        for a in clubdb.all_awards(conn):
            stem = f"{a['year'] or 'na'}-{slugify(a['name'] or 'award')}"
            emit_json("awards", f"{stem}.json", _award_doc(a))
        for r in clubdb.all_reviews(conn):
            name = f"{r['book_slug']}--{r['member_slug']}.md"
            (out_root / "reviews" / name).write_text(_review_text(r))
            keep["reviews"].add(name)
            written["reviews"] += 1

    written["_pruned"] = sum(_prune(out_root / d, keep[d]) for d in ENTITY_DIRS)
    return written


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output corpus root")
    args = ap.parse_args()
    written = generate(Path(args.out))
    print(json.dumps(written, indent=2))


if __name__ == "__main__":
    main()
