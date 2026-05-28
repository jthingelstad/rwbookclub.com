"""What proactive notifications are due right now — pure logic, no Discord.

`due_notifications(now, already_sent)` reads the corpus and returns `[(key, message)]`
for meeting reminders, a review nudge, milestones, and the anniversary, filtering out
anything whose key was already sent. The bot's daily loop posts the new ones and records
their keys. Conservative by design: each key fires once, and only for real events.
"""

from __future__ import annotations

from datetime import datetime

from agent import corpus_read as cr

FOUNDED_YEAR = 2003
ANNIVERSARY_MONTH = 4  # founded April 2003
REMIND_WITHIN_DAYS = 3
NUDGE_WITHIN_DAYS = 30


def _parse(dt: str | None) -> datetime | None:
    if not dt:
        return None
    try:
        return datetime.fromisoformat(dt.replace("Z", "+00:00"))
    except ValueError:
        return None


def due_notifications(now: datetime, already_sent: set[str]) -> list[tuple[str, str]]:
    books = cr.books()
    read = [b for b in books if b.get("meetingDate") and not b.get("placeholder")]
    out: list[tuple[str, str]] = []

    # 1. Upcoming-meeting reminders — placeholder meetings within the window.
    for b in books:
        md = _parse(b.get("meetingDate"))
        if not b.get("placeholder") or not md:
            continue
        days = (md - now).total_seconds() / 86400
        if 0 <= days <= REMIND_WITHIN_DAYS:
            authors = ", ".join(b.get("authors") or []) or "—"
            picker = f", picked by {b['pickerName']}" if b.get("pickerName") else ""
            out.append((
                f"meeting-{b['slug']}-soon",
                f"📅 Next up: **{b['title']}** by {authors} on {b['meetingDate'][:10]}{picker}. See you there!",
            ))

    # 2. Review nudge — one gentle prompt for the most recently read book.
    if read:
        recent = max(read, key=lambda b: b.get("meetingDate") or "")
        md = _parse(recent.get("meetingDate"))
        if md and 0 <= (now - md).total_seconds() / 86400 <= NUDGE_WITHIN_DAYS:
            out.append((
                f"review-nudge-{recent['slug']}",
                f"📚 We just read **{recent['title']}** — log your take any time with `/oliver review`.",
            ))

    # 3. Milestone — books read at an exact multiple of 25.
    n = len(read)
    if n and n % 25 == 0:
        out.append((
            f"milestone-books-{n}",
            f"🎉 That's **{n} books** read since April 2003. Onward!",
        ))

    # 4. Anniversary — during the founding month.
    if now.month == ANNIVERSARY_MONTH:
        out.append((
            f"anniversary-{now.year}",
            f"🎂 It's our anniversary month — **{now.year - FOUNDED_YEAR} years** of the R/W Book Club!",
        ))

    return [(k, m) for k, m in out if k not in already_sent]
