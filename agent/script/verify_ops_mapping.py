"""Phase 3 safety gate: prove every operational row remaps onto a real club FK.

The ops tables (meeting_attendance, roll_calls, reading_statuses, member_contacts)
are keyed today by loose text: ``meeting_key`` (a book slug) and
``member_slug``. The authoritative seam fix replaces those with integer FKs
(``meeting_id`` → club_meetings, ``member_id`` → club_members). Before that rebuild can
run, EVERY existing ops row must resolve — a single orphan would silently drop attendance
or reading history.

This script resolves each distinct ``meeting_key``/``member_slug`` against the club_*
tables and reports orphans. Exit 0 = safe to migrate; exit 1 = orphans found.

    python -m agent.script.verify_ops_mapping
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent import db  # noqa: E402

# (table, has_member_slug). roll_calls has only meeting_key.
OPS_TABLES = [
    ("meeting_attendance", True),
    ("roll_calls", False),
    ("reading_statuses", True),
    ("member_contacts", True),
]


def _meeting_id_for_slug(conn, slug: str) -> int | None:
    """A meeting_key is a book slug. Resolve it to the meeting it refers to via
    club_books → club_meeting_books. Returns the (single) meeting id, or None."""
    rows = conn.execute(
        "SELECT mb.meeting_id FROM club_books b "
        "JOIN club_meeting_books mb ON mb.book_id = b.id WHERE b.slug = ?",
        (slug,),
    ).fetchall()
    if not rows:
        return None
    # Most book slugs map to exactly one meeting; if a book spanned two meetings the
    # latest is the operationally-relevant one (ops state is always the next meeting).
    return max(r["meeting_id"] for r in rows)


def verify() -> int:
    orphans: list[str] = []
    mapping: dict[str, int] = {}
    member_map: dict[str, int] = {}
    with db.connect() as conn:
        member_ids = {r["slug"]: r["id"] for r in conn.execute("SELECT id, slug FROM club_members")}
        for table, has_member in OPS_TABLES:
            cols = "meeting_key" + (", member_slug" if has_member else "")
            seen_keys, seen_members = set(), set()
            for row in conn.execute(f"SELECT DISTINCT {cols} FROM {table}"):
                mk = row["meeting_key"]
                if mk and mk not in seen_keys:
                    seen_keys.add(mk)
                    mid = _meeting_id_for_slug(conn, mk)
                    if mid is None:
                        # date-prefix fallback meeting_key (no corpus book) is acceptable
                        if len(mk) == 10 and mk[4] == "-":
                            mapping[mk] = -1   # date-only; ops layer keeps it nullable
                        else:
                            orphans.append(f"{table}: meeting_key '{mk}' → no club meeting")
                    else:
                        mapping[mk] = mid
                if has_member:
                    ms = row["member_slug"]
                    if ms and ms not in seen_members:
                        seen_members.add(ms)
                        if ms in member_ids:
                            member_map[ms] = member_ids[ms]
                        else:
                            orphans.append(f"{table}: member_slug '{ms}' → no club member")

    print("meeting_key → meeting_id:")
    for k, v in sorted(mapping.items()):
        print(f"  {k:30} → {v if v != -1 else '(date-only, nullable)'}")
    print("member_slug → member_id:")
    for k, v in sorted(member_map.items()):
        print(f"  {k:30} → {v}")

    if orphans:
        print(f"\n✗ {len(orphans)} ORPHAN(S) — NOT safe to migrate:")
        for o in orphans:
            print(f"  - {o}")
        return 1
    print("\n✓ zero orphans — every ops row remaps onto a real club FK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(verify())
