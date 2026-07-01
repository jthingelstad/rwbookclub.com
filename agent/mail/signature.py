"""Oliver's contextual email signature: the next read + a rotating club fun fact.

Outbound club emails close with this instead of a bare "Oliver", so every message
carries a little of the club's character. Everything is computed from the corpus —
the facts are always accurate — and the fun fact rotates for variety.
"""

from __future__ import annotations

import html
import random
from datetime import date

from agent import clock, config
from agent import corpus_read as cr
from agent.club import meeting_rules


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


def _sig_snapshot(*, today: date | None = None, rng: random.Random | None = None) -> dict:
    """One snapshot of the sign-off data (next read + one rotating fact) so the plain-text and
    HTML signatures always show the same thing. `today`/`rng` are injectable for tests."""
    today = today or clock.club_today()
    rng = rng or random
    upcoming = cr.upcoming_meetings()
    facts = _fun_facts(cr.club_stats(), cr.books(), today)
    return {"next": upcoming[0] if upcoming else None,
            "fact": rng.choice(facts) if facts else None}


def _next_up_text(nxt: dict) -> str:
    when = meeting_rules.friendly_when(nxt.get("meetingDate"), nxt.get("startTime"))
    picker = f", picked by {nxt['pickedBy']}" if nxt.get("pickedBy") else ""
    tail = f" on {when}" if when else ""
    loc = f" ({nxt['location']})" if nxt.get("location") else ""
    return f"📚 Next up: {nxt['title']}{picker}{tail}{loc}."


def _text_from_snapshot(snap: dict) -> str:
    lines = ["— Oliver"]
    if snap["next"]:
        lines.append(_next_up_text(snap["next"]))
    if snap["fact"]:
        lines.append(snap["fact"])
    return "\n".join(lines)


def email_signature_html(snap: dict) -> str:
    """A small styled HTML sign-off — links Oliver to the site and the next book to its page — so
    the signature owns its own HTML formatting instead of leaning on the renderer's nl2br."""
    site = config.SITE_URL
    lines = [f'<p>— <a href="{site}/">Oliver</a></p>']
    nxt = snap["next"]
    if nxt:
        title = f"<em>{html.escape(nxt.get('title') or '')}</em>"
        slug = nxt.get("slug")
        title_html = f'<a href="{site}/books/{slug}/">{title}</a>' if slug else title
        when = meeting_rules.friendly_when(nxt.get("meetingDate"), nxt.get("startTime"))
        picker = f", picked by {html.escape(nxt['pickedBy'])}" if nxt.get("pickedBy") else ""
        tail = f" on {html.escape(when)}" if when else ""
        loc = f" ({html.escape(nxt['location'])})" if nxt.get("location") else ""
        lines.append(f"<p>📚 Next up: {title_html}{picker}{tail}{loc}.</p>")
    if snap["fact"]:
        lines.append(f'<p class="oliver-sig-fact">{html.escape(snap["fact"])}</p>')
    return '<div class="oliver-sig">' + "".join(lines) + "</div>"


def email_signature(*, today: date | None = None, rng: random.Random | None = None) -> str:
    """Plain-text sign-off (the text/plain part + previews): '— Oliver', the next read, one fact."""
    return _text_from_snapshot(_sig_snapshot(today=today, rng=rng))


def email_signatures(*, today: date | None = None,
                     rng: random.Random | None = None) -> tuple[str, str]:
    """(plain_text, html) sign-offs from ONE snapshot — the send path uses this so both MIME parts
    show the same next book and fun fact."""
    snap = _sig_snapshot(today=today, rng=rng)
    return _text_from_snapshot(snap), email_signature_html(snap)
