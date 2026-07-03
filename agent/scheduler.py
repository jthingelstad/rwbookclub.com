"""What proactive notifications are due right now — pure logic, no Discord, no LLM.

`due_notifications(now, already_sent)` reads the corpus and returns a list of
`Notification`s for meeting reminders, a review nudge, milestones, and the
anniversary, filtering out anything whose key was already sent. Each notification
carries the structured `facts` for Oliver to voice (via oliver.compose) plus a
literal `fallback` template the caller uses if the LLM is unavailable. The bot's
daily loop posts the new ones and records their keys. Conservative by design: each
key fires once, and only for real events.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from agent import clock
from agent import corpus_read as cr

FOUNDED_YEAR = 2003
ANNIVERSARY_MONTH = 4  # founded April 2003
REMIND_WITHIN_DAYS = 3
NUDGE_WITHIN_DAYS = 30


@dataclass(frozen=True)
class Notification:
    key: str       # dedup key — fires once
    kind: str      # short description handed to oliver.compose
    facts: dict    # authoritative facts for Oliver to voice
    fallback: str  # literal template, used when the LLM is unavailable


def _parse(dt: str | None) -> datetime | None:
    """Parse a meeting date/datetime to an aware datetime. Meeting dates are now LOCAL
    ('YYYY-MM-DD', America/Chicago) — a bare date parses naive, so attach the club tz so
    arithmetic against an aware `now` works (legacy 'Z' datetimes stay UTC-aware)."""
    if not dt:
        return None
    try:
        parsed = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=clock.tz())  # club tz (config.CLUB_TIMEZONE), not a literal
    return parsed


def due_notifications(now: datetime, already_sent: set[str]) -> list[Notification]:
    books = cr.books()
    read = [b for b in books if b.get("isRead")]
    out: list[Notification] = []

    # 1. Upcoming-meeting reminders — upcoming meetings within the window.
    for b in books:
        md = _parse(b.get("meetingDate"))
        if not b.get("isUpcoming") or not md:
            continue
        days = (md - now).total_seconds() / 86400
        if 0 <= days <= REMIND_WITHIN_DAYS:
            authors = ", ".join(b.get("authors") or []) or "—"
            picker = f", picked by {b['pickerName']}" if b.get("pickerName") else ""
            out.append(Notification(
                key=f"meeting-{b['slug']}-soon",
                kind="short, warm meeting reminder for the club channel",
                facts={
                    "occasion": "the next club meeting is coming up soon",
                    "book": b["title"],
                    "authors": authors,
                    "meeting date": b["meetingDate"][:10],
                    "picker": b.get("pickerName") or None,
                },
                fallback=f"📅 Next up: **{b['title']}** by {authors} on {b['meetingDate'][:10]}{picker}. See you there!",
            ))

    # 2. Review nudge — one gentle prompt for the most recently read book.
    if read:
        recent = max(read, key=lambda b: b.get("meetingDate") or "")
        md = _parse(recent.get("meetingDate"))
        if md and 0 <= (now - md).total_seconds() / 86400 <= NUDGE_WITHIN_DAYS:
            out.append(Notification(
                key=f"review-nudge-{recent['slug']}",
                kind="gentle nudge to log a review of the book the club just read",
                facts={
                    "occasion": "the club recently finished this book",
                    "book": recent["title"],
                    "how to log a review": "open the web app — run /oliver my-club for a private link, then the Ratings or Reviews tab",
                },
                fallback=f"📚 We just read *{recent['title']}* — rate it or log your take any time: run `/oliver my-club` for your private link.",
            ))

    # 3. Milestone — books read at an exact multiple of 25.
    n = len(read)
    if n and n % 25 == 0:
        out.append(Notification(
            key=f"milestone-books-{n}",
            kind="celebratory milestone note for the club",
            facts={"milestone": f"{n} books read since the club began in April 2003"},
            fallback=f"🎉 That's **{n} books** read since April 2003. Onward!",
        ))

    # 4. Anniversary — during the founding month.
    if now.month == ANNIVERSARY_MONTH:
        years = now.year - FOUNDED_YEAR
        out.append(Notification(
            key=f"anniversary-{now.year}",
            kind="anniversary note for the club",
            facts={"anniversary": f"{years} years of the R/W Book Club", "founded": "April 2003"},
            fallback=f"🎂 It's our anniversary month — **{years} years** of the R/W Book Club!",
        ))

    return [n for n in out if n.key not in already_sent]
