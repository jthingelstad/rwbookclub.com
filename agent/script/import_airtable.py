"""One-time bootstrap: load the authoritative club record into SQLite (``club_*`` tables).

Hybrid source, by design:
  * **Integer ids + dropped contact fields + meeting hosts come from Airtable** — it owns
    the autonumber surrogate keys (Member/Author/Review ID; Book/Meeting ID also echoed in
    the corpus) and the typed fields the corpus normalization dropped (member Email/Mobile),
    and the meeting Host link (which the corpus never carried).
  * **Every live scalar value and slug-relationship comes from the corpus** — it is current
    and is exactly what the website + Oliver render today (it has even been hand-curated
    beyond Airtable, e.g. The Overstory's topic). Loading scalars from the corpus is what
    makes the regenerated corpus faithful (diff-clean).

Airtable is read from the local cache under ``agent/script/_airtable_cache/`` (captured
once; re-fetch by deleting the cache). The corpus is read from ``corpus/data/``.

Idempotent: wipes the ``club_*`` tables and reloads. Run:
    python -m agent.script.import_airtable           # import + validate
    python -m agent.script.import_airtable --report   # validate only, no write
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent import clubdb, db  # noqa: E402

CACHE_DIR = REPO_ROOT / "agent" / "script" / "_airtable_cache"
DATA_DIR = REPO_ROOT / "corpus" / "data"


# ── loaders ──────────────────────────────────────────────────────────────────
def _airtable(name: str) -> list[dict]:
    return json.loads((CACHE_DIR / f"{name}.json").read_text())


def _corpus_json(subdir: str) -> dict[str, dict]:
    """{slug(stem): record} for a corpus dir of .json files."""
    out = {}
    for p in sorted((DATA_DIR / subdir).glob("*.json")):
        out[p.stem] = json.loads(p.read_text())
    return out


_FM = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.S)


def _corpus_reviews() -> list[dict]:
    import yaml
    out = []
    for p in sorted((DATA_DIR / "reviews").glob("*.md")):
        m = _FM.match(p.read_text())
        if not m:
            continue
        front = yaml.safe_load(m.group(1)) or {}
        front["_body"] = m.group(2).strip()
        out.append(front)
    return out


# ── Airtable id maps ─────────────────────────────────────────────────────────
def _build_airtable_maps() -> dict:
    members = _airtable("members")
    authors = _airtable("authors")
    reviews = _airtable("reviews")
    meetings = _airtable("meetings")

    member_rec_to_id = {r["id"]: r["fields"]["Member ID"] for r in members}
    member_name_to_id = {r["fields"]["Name"]: r["fields"]["Member ID"] for r in members}
    member_contact = {
        r["fields"]["Member ID"]: {
            "email": (r["fields"].get("Email Address") or None),
            "mobile": (r["fields"].get("Mobile") or None),
        }
        for r in members
    }
    author_name_to_id: dict[str, int] = {}
    for r in authors:
        author_name_to_id[r["fields"]["Author"]] = r["fields"]["Author ID"]
    # review rec-id (corpus frontmatter `id`) -> Review ID autonumber
    review_rec_to_id = {r["id"]: r["fields"]["Review ID"] for r in reviews}
    # meeting hosts: Meeting ID -> [Member ID...] (host rec ids resolved)
    meeting_hosts: dict[int, list[int]] = {}
    for r in meetings:
        mid = r["fields"]["Meeting ID"]
        hosts = [member_rec_to_id[h] for h in r["fields"].get("Host", []) if h in member_rec_to_id]
        if hosts:
            meeting_hosts[mid] = hosts
    return {
        "member_name_to_id": member_name_to_id,
        "member_contact": member_contact,
        "author_name_to_id": author_name_to_id,
        "review_rec_to_id": review_rec_to_id,
        "meeting_hosts": meeting_hosts,
    }


# ── import ───────────────────────────────────────────────────────────────────
def run_import(*, write: bool = True) -> dict:
    clubdb.ensure_schema()
    at = _build_airtable_maps()
    warnings: list[str] = []

    members = _corpus_json("members")          # slug -> {name, isCurrent, websites[]}
    authors = _corpus_json("authors")          # slug -> {name, bio?}
    books = _corpus_json("books")              # slug -> full book record
    meetings = _corpus_json("meetings")        # stem -> meeting record (has meetingId)
    awards = _corpus_json("awards")            # stem -> award record
    reviews = _corpus_reviews()                # list of frontmatter dicts (+ _body)

    # Resolve integer ids for members/authors (corpus has none) via Airtable name maps.
    member_id_for_slug: dict[str, int] = {}
    member_rows = []
    email_identity_rows = []     # member emails -> member_identities(surface='email')
    website_identity_rows = []   # member websites -> member_identities(surface='website')
    for slug, m in members.items():
        mid = at["member_name_to_id"].get(m["name"])
        if mid is None:
            warnings.append(f"member '{slug}' ({m['name']}) has no Airtable Member ID — skipped")
            continue
        member_id_for_slug[slug] = mid
        member_rows.append((mid, slug, m["name"], 1 if m.get("isCurrent") else 0))
        contact = at["member_contact"].get(mid, {})
        if contact.get("email"):
            email_identity_rows.append(("email", contact["email"].strip().lower(), mid, 1, "airtable_import"))
        # websites now live in member_identities (tolerate the legacy single-string corpus too)
        member_websites = m.get("websites") or ([m["website"]] if m.get("website") else [])
        for i, url in enumerate(member_websites):
            website_identity_rows.append(("website", url, mid, 1 if i == 0 else 0, "airtable_import"))

    author_id_for_slug: dict[str, int] = {}
    author_rows = []
    for slug, a in authors.items():
        aid = at["author_name_to_id"].get(a["name"])
        if aid is None:
            warnings.append(f"author '{slug}' ({a['name']}) has no Airtable Author ID — skipped")
            continue
        author_id_for_slug[slug] = aid
        author_rows.append((aid, slug, a["name"], a.get("bio")))

    # name -> author id (for book.authors which are names, not slugs)
    author_id_for_name = {a["name"]: author_id_for_slug.get(slug)
                          for slug, a in authors.items() if slug in author_id_for_slug}

    book_id_for_slug: dict[str, int] = {}
    book_rows = []
    book_author_rows = []
    book_picker_rows = []
    for slug, b in books.items():
        bid = b["bookId"]
        book_id_for_slug[slug] = bid
        subjects_json = json.dumps(b["subjects"], ensure_ascii=False) if "subjects" in b else None
        book_rows.append((bid, slug, b["title"], b.get("subtitle"), b.get("topic"),
                          1 if b.get("fiction") else 0, b.get("publicationYear"),
                          b.get("pageCount"), b.get("isbn13"), b.get("olKey"),
                          b.get("synopsis"), subjects_json))
        for i, name in enumerate(b.get("authors") or []):
            aid = author_id_for_name.get(name)
            if aid is None:
                warnings.append(f"book '{slug}' author '{name}' did not resolve to an author id")
                continue
            book_author_rows.append((bid, aid, i))
        seen_pickers = set()
        ordinal = 0
        for ps in b.get("picker") or []:
            mid = member_id_for_slug.get(ps)
            if mid is None:
                warnings.append(f"book '{slug}' picker slug '{ps}' did not resolve to a member id")
                continue
            if mid in seen_pickers:   # de-dupe the ['dan','dan'] quirk
                continue
            seen_pickers.add(mid)
            book_picker_rows.append((bid, mid, ordinal)); ordinal += 1

    meeting_rows = []
    meeting_book_rows = []
    meeting_host_rows = []
    for stem, mt in meetings.items():
        mid = mt["meetingId"]
        meeting_rows.append((mid, mt.get("date"), mt.get("startTime"),
                             json.dumps(mt.get("type") or [], ensure_ascii=False),
                             mt.get("location"), mt.get("notes"),
                             1 if mt.get("placeholder") else 0))
        for i, bslug in enumerate(mt.get("books") or []):
            bid = book_id_for_slug.get(bslug)
            if bid is None:
                warnings.append(f"meeting '{stem}' book slug '{bslug}' did not resolve")
                continue
            meeting_book_rows.append((mid, bid, i))
        for i, host_mid in enumerate(at["meeting_hosts"].get(mid, [])):
            meeting_host_rows.append((mid, host_mid, i))

    review_rows = []
    for r in reviews:
        rec = r.get("id")
        rid = at["review_rec_to_id"].get(rec)
        if rid is None:
            warnings.append(f"review {rec} (book={r.get('book')}) not in Airtable — dropped (Oliver test write)")
            continue
        bid = book_id_for_slug.get(r.get("book"))
        mid = member_id_for_slug.get(r.get("member"))
        if bid is None or mid is None:
            warnings.append(f"review {rec} book/member slug did not resolve — skipped")
            continue
        review_rows.append((rid, bid, mid, r.get("rating"),
                            1 if r.get("dnf") else 0, r.get("discussionQuality"),
                            1 if r.get("wouldRecommend") else 0, r.get("favoriteQuote"),
                            r.get("_body") or None, r.get("createdAt")))

    award_rows = []
    award_book_rows = []
    award_voter_rows = []
    for i, (stem, aw) in enumerate(sorted(awards.items()), start=1):
        aid = i  # minted
        award_rows.append((aid, aw.get("name"), aw.get("year"), aw.get("award"), aw.get("notes")))
        for bslug in aw.get("books") or []:
            bid = book_id_for_slug.get(bslug)
            if bid is not None:
                award_book_rows.append((aid, bid))
        for vslug in aw.get("voters") or []:
            mid = member_id_for_slug.get(vslug)
            if mid is not None:
                award_voter_rows.append((aid, mid))

    result = {
        "rows": {
            "club_members": len(member_rows), "club_authors": len(author_rows),
            "club_books": len(book_rows), "club_book_authors": len(book_author_rows),
            "club_book_pickers": len(book_picker_rows), "club_meetings": len(meeting_rows),
            "club_meeting_books": len(meeting_book_rows), "club_meeting_hosts": len(meeting_host_rows),
            "club_reviews": len(review_rows), "club_awards": len(award_rows),
            "club_award_books": len(award_book_rows), "club_award_voters": len(award_voter_rows),
        },
        "warnings": warnings,
    }
    if not write:
        return result

    with db.connect() as conn:
        conn.execute("DELETE FROM member_identities WHERE surface IN ('email', 'website')")  # FK to club_members
        for t in reversed(clubdb.CLUB_TABLES):
            conn.execute(f"DELETE FROM {t}")
        conn.executemany("INSERT INTO club_members(id,slug,name,is_current) VALUES (?,?,?,?)", member_rows)
        conn.executemany(
            "INSERT OR REPLACE INTO member_identities(surface,identifier,member_id,is_primary,linked_by) "
            "VALUES (?,?,?,?,?)", email_identity_rows + website_identity_rows)
        conn.executemany("INSERT INTO club_authors(id,slug,name,bio) VALUES (?,?,?,?)", author_rows)
        conn.executemany("INSERT INTO club_books(id,slug,title,subtitle,topic,fiction,publication_year,page_count,isbn13,ol_key,synopsis,subjects_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", book_rows)
        conn.executemany("INSERT INTO club_book_authors(book_id,author_id,ordinal) VALUES (?,?,?)", book_author_rows)
        conn.executemany("INSERT INTO club_book_pickers(book_id,member_id,ordinal) VALUES (?,?,?)", book_picker_rows)
        conn.executemany("INSERT INTO club_meetings(id,date,start_time,type_json,location,notes,placeholder) VALUES (?,?,?,?,?,?,?)", meeting_rows)
        conn.executemany("INSERT INTO club_meeting_books(meeting_id,book_id,ordinal) VALUES (?,?,?)", meeting_book_rows)
        conn.executemany("INSERT INTO club_meeting_hosts(meeting_id,member_id,ordinal) VALUES (?,?,?)", meeting_host_rows)
        conn.executemany("INSERT INTO club_reviews(id,book_id,member_id,rating,dnf,discussion_quality,would_recommend,favorite_quote,body,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", review_rows)
        conn.executemany("INSERT INTO club_awards(id,name,year,award_category,notes) VALUES (?,?,?,?,?)", award_rows)
        conn.executemany("INSERT INTO club_award_books(award_id,book_id) VALUES (?,?)", award_book_rows)
        conn.executemany("INSERT INTO club_award_voters(award_id,member_id) VALUES (?,?)", award_voter_rows)
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="dry run: compute + validate, no write")
    args = ap.parse_args()
    result = run_import(write=not args.report)
    print(json.dumps(result["rows"], indent=2))
    if result["warnings"]:
        print(f"\n{len(result['warnings'])} warning(s):")
        for w in result["warnings"]:
            print(f"  - {w}")
    else:
        print("\nno warnings — every reference resolved.")
    if not args.report:
        print("\nfinal table counts:")
        print(json.dumps(clubdb.counts(), indent=2))


if __name__ == "__main__":
    main()
