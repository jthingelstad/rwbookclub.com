"""Generate the corpus (``corpus/data/*``, gitignored) from the authoritative ``club_*`` tables.

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
from corpus.paths import DATA_DIR  # noqa: E402

# The corpus is a private, on-disk artifact; DATA_DIR honors OLIVER_CORPUS_DIR so a test run
# regenerates into a temp dir instead of the developer's real corpus/data.
DEFAULT_OUT = DATA_DIR
ENTITY_DIRS = ["books", "meetings", "members", "authors", "reviews", "lists"]


def _write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def _add(doc: dict, key: str, value) -> None:
    """Append an enrichment key only when it carries a real value — keeps the
    corpus files clean (and diffs small) for entities that didn't enrich."""
    if value not in (None, "", [], {}):
        doc[key] = value


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
    # External enrichment (club_book_enrichment) — omitted when empty.
    _add(doc, "editionCount", b.get("edition_count"))
    _add(doc, "languages", b.get("languages"))
    _add(doc, "ratingsAverage", b.get("ratings_average"))
    _add(doc, "ratingsCount", b.get("ratings_count"))
    _add(doc, "series", b.get("series"))
    _add(doc, "awards", b.get("awards"))          # literary awards (≠ club_award_*)
    _add(doc, "wikidataId", b.get("wikidata_id"))
    _add(doc, "wikipediaUrl", b.get("wikipedia_url"))
    _add(doc, "goodreadsId", b.get("goodreads_id"))
    return doc


def _meeting_doc(m: dict) -> dict:
    return {
        "meetingId": m["id"],
        "date": m["date"],                 # LOCAL date 'YYYY-MM-DD' (America/Chicago)
        "startTime": m["start_time"],      # LOCAL 'HH:MM' or null
        "books": m["book_slugs"],
        "host": m["host_slugs"],           # who hosted (meeting-level; ≠ a book's picker)
        "type": m["type"],
        "location": m["location"],
        "notes": m["notes"],
    }


def _member_doc(m: dict) -> dict:
    # `websites` are sourced from member_identities (surface='website') and attached to `m` by the
    # generate() loop — multiple per member, public, rendered on the profile page. Emails/phones are
    # private and never enter the corpus.
    return {"name": m["name"], "isCurrent": bool(m["is_current"]),
            "joined": m.get("joined"), "websites": m.get("websites") or []}


def _author_doc(a: dict) -> dict:
    doc = {"name": a["name"]}
    if a["bio"]:                          # bio omitted (not null) when empty
        doc["bio"] = a["bio"]
    # External enrichment (club_author_enrichment) — omitted when empty. The portrait
    # itself stays a filesystem asset (assets/images/authors/); photoCredit carries
    # its attribution for display.
    _add(doc, "birthYear", a.get("birth_year"))
    _add(doc, "deathYear", a.get("death_year"))
    _add(doc, "nationality", a.get("nationality"))
    _add(doc, "website", a.get("website"))
    _add(doc, "wikipediaUrl", a.get("wikipedia_url"))
    _add(doc, "notableWorks", a.get("notable_works"))
    _add(doc, "photoCredit", a.get("photo_credit"))
    return doc


def _list_doc(lst: dict) -> dict:
    # Public list record: ordered books with optional per-book notes. `owner` is the member slug
    # (null for club lists); `description` is omitted when empty.
    doc = {
        "name": lst["name"],
        "scope": lst["scope"],
        "owner": lst.get("owner_slug"),
        "books": [
            {"book": e["book_slug"], **({"note": e["note"]} if e.get("note") else {})}
            for e in lst["entries"]
        ],
    }
    _add(doc, "description", lst.get("description"))
    return doc


def _review_text(r: dict) -> str:
    front = {
        "id": r["id"],
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
        websites_by_id: dict[int, list[dict]] = {}
        for r in conn.execute(
            "SELECT member_id, identifier, label FROM member_identities WHERE surface = 'website' "
            "ORDER BY is_primary DESC, created_at, identifier"
        ):
            websites_by_id.setdefault(r["member_id"], []).append(
                {"url": r["identifier"], "label": r["label"]})
        for m in clubdb.all_members(conn):
            doc_src = dict(m)
            doc_src["websites"] = websites_by_id.get(m["id"], [])
            emit_json("members", f"{m['slug']}.json", _member_doc(doc_src))
        for a in clubdb.all_authors(conn):
            emit_json("authors", f"{a['slug']}.json", _author_doc(a))
        for lst in clubdb.all_lists(conn):
            emit_json("lists", f"{lst['slug']}.json", _list_doc(lst))
        for r in clubdb.all_reviews(conn):
            name = f"{r['book_slug']}--{r['member_slug']}.md"
            (out_root / "reviews" / name).write_text(_review_text(r))
            keep["reviews"].add(name)
            written["reviews"] += 1

    written["_pruned"] = sum(_prune(out_root / d, keep[d]) for d in ENTITY_DIRS)
    return written


# ── Targeted single-entity writers (used by the DB-backed write path) ─────────
def write_book_file(conn, book_id: int, out_root: Path = DEFAULT_OUT) -> Path:
    b = next(b for b in clubdb.all_books(conn) if b["id"] == book_id)
    path = Path(out_root) / "books" / f"{b['slug']}.json"
    _write_json(path, _book_doc(b))
    return path


def write_author_file(conn, author_id: int, out_root: Path = DEFAULT_OUT) -> Path:
    a = next(a for a in clubdb.all_authors(conn) if a["id"] == author_id)
    path = Path(out_root) / "authors" / f"{a['slug']}.json"
    _write_json(path, _author_doc(a))
    return path


def write_meeting_file(conn, meeting_id: int, out_root: Path = DEFAULT_OUT) -> Path:
    m = next(m for m in clubdb.all_meetings(conn) if m["id"] == meeting_id)
    stem = f"{(m['date'] or 'undated')[:10]}--{m['id']}"
    path = Path(out_root) / "meetings" / f"{stem}.json"
    _write_json(path, _meeting_doc(m))
    return path


def write_review_file(conn, review_id: int, out_root: Path = DEFAULT_OUT) -> Path:
    r = next(r for r in clubdb.all_reviews(conn) if r["id"] == review_id)
    path = Path(out_root) / "reviews" / f"{r['book_slug']}--{r['member_slug']}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_review_text(r))
    return path


def write_list_file(conn, list_id: int, out_root: Path = DEFAULT_OUT) -> Path:
    lst = next(x for x in clubdb.all_lists(conn) if x["id"] == list_id)
    path = Path(out_root) / "lists" / f"{lst['slug']}.json"
    _write_json(path, _list_doc(lst))
    return path


def remove_list_file(slug: str, out_root: Path = DEFAULT_OUT) -> None:
    """Delete a list's corpus file on list deletion (full regen would also prune it)."""
    (Path(out_root) / "lists" / f"{slug}.json").unlink(missing_ok=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output corpus root")
    args = ap.parse_args()
    written = generate(Path(args.out))
    print(json.dumps(written, indent=2))


if __name__ == "__main__":
    main()
