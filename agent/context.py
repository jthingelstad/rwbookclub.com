"""The compact club overview Oliver carries in his (cached) system prompt.

Not the whole corpus — just enough for at-a-glance grounding (totals, topic mix,
current roster, what's next). Oliver pulls specifics on demand via tools
(agent/tools.py over agent/corpus_read.py).
"""

from __future__ import annotations

from collections import Counter

from agent import corpus_read as cr


def book_count() -> int:
    return len(cr.books())


def _picks_by_slug() -> Counter:
    """Career pick count per member slug (member JSON files don't store it —
    it's derived from books.picker, same as corpus_read.member_history)."""
    picks: Counter = Counter()
    for b in cr.books():
        for slug in (b.get("picker") or []):
            picks[slug] += 1
    return picks


def club_context() -> str:
    stats = cr.club_stats()
    picks = _picks_by_slug()
    current = sorted(
        (m for m in cr.members() if m.get("isCurrent")),
        key=lambda m: picks[m.get("slug")],
        reverse=True,
    )
    upcoming = cr.upcoming_meetings()

    lines = [
        "THE R/W BOOK CLUB — overview",
        "Meeting monthly since April 2003 in Minneapolis–Saint Paul; about 8 books a year, "
        "mostly non-fiction. Members rotate picking the book and hosting the discussion.",
        f"Read {stats['totalRead']} books so far "
        f"({stats['nonfiction']} non-fiction, {stats['fiction']} fiction), "
        f"{stats['firstYear']}–{stats['lastYear']}.",
        "Top topics: " + ", ".join(f"{t} ({n})" for t, n in stats["topics"][:6]) + ".",
        "Current members (by picks): "
        + ", ".join(f"{m['name']} ({picks[m.get('slug')]})" for m in current) + ".",
    ]
    if upcoming:
        nxt = "; ".join(
            f"{u['title']} ({(u.get('meetingDate') or '')[:7] or 'TBD'}"
            f"{', picked by ' + u['pickedBy'] if u.get('pickedBy') else ''})"
            for u in upcoming[:4]
        )
        lines.append("Upcoming: " + nxt + ".")
    return "\n".join(lines)
