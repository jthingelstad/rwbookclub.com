"""Oliver's contextual email signature: the next read + a rotating club fun fact.

Outbound club emails close with this instead of a bare "Oliver", so every message
carries a little of the club's character. Everything is computed from the corpus —
the facts are always accurate — and the fun fact rotates for variety.
"""

from __future__ import annotations

import random
from datetime import date

from agent import corpus_read as cr


def _fun_facts(stats: dict, books: list[dict], today: date) -> list[str]:
    """Candidate one-liners, all true, drawn from the corpus."""
    facts: list[str] = []
    total = stats.get("totalRead") or 0
    if total:
        facts.append(f"That's {total} books read together since 2003.")
    years = today.year - 2003
    if years > 0:
        facts.append(f"We've been meeting for {years} years and counting.")
    if stats.get("nonfiction") and stats.get("fiction"):
        facts.append(f"We lean hard non-fiction — {stats['nonfiction']} to {stats['fiction']}.")
    pages = stats.get("totalPages") or 0
    if pages:
        facts.append(f"Roughly {pages:,} pages read between us so far.")
    leaders = stats.get("pickerLeaderboard") or []
    if leaders and leaders[0][0] and leaders[0][1]:
        facts.append(f"{leaders[0][0]} has picked the most over the years ({leaders[0][1]}).")
    # "N years ago this month we read X" — a past read in the same calendar month.
    mm = f"-{today.month:02d}-"
    prior = [
        b for b in books
        if b.get("isRead") and mm in (b.get("meetingDate") or "")
        and (b.get("meetingDate") or "")[:4].isdigit()
        and int(b["meetingDate"][:4]) < today.year
    ]
    if prior:
        b = max(prior, key=lambda x: x["meetingDate"])  # most recent prior-year match
        ago = today.year - int(b["meetingDate"][:4])
        facts.append(f"{ago} year{'s' if ago != 1 else ''} ago this month we read {b.get('title')}.")
    return facts


def email_signature(*, today: date | None = None, rng: random.Random | None = None) -> str:
    """A short sign-off: '— Oliver', the next read, and one rotating fun fact.

    `today`/`rng` are injectable for deterministic tests; both default to live values.
    """
    today = today or date.today()
    rng = rng or random
    lines = ["— Oliver"]

    upcoming = cr.upcoming_meetings()
    if upcoming:
        nxt = upcoming[0]
        when = (nxt.get("meetingDate") or "")[:10]
        picker = f", picked by {nxt['pickedBy']}" if nxt.get("pickedBy") else ""
        tail = f" on {when}" if when else ""
        lines.append(f"📚 Next up: {nxt['title']}{picker}{tail}.")

    facts = _fun_facts(cr.club_stats(), cr.books(), today)
    if facts:
        lines.append(rng.choice(facts))

    return "\n".join(lines)
