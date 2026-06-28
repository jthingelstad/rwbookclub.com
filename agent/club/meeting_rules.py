"""Rules and status helpers for R/W Book Club meeting attendance.

Oliver is allowed to run roll call and flag rule conflicts, but not to decide
the schedule. The club rule stays explicit here:

- meetings normally land on the last Tuesday of the month
- at least 3 of the 5 current members need to attend
- the book picker must be able to attend
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from agent import clubdb, corpus_read, db

QUORUM_REQUIRED = 3
MEETING_WEEKDAY = 1  # Tuesday, where Monday is 0.


def last_tuesday(year: int, month: int) -> date:
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - MEETING_WEEKDAY) % 7)


def friendly_date(iso: str | None) -> str:
    """ISO date → 'Tuesday, June 30' (how a person writes it), or '' if unparseable."""
    try:
        d = date.fromisoformat((iso or "")[:10])
    except ValueError:
        return iso or ""
    return f"{d.strftime('%A, %B')} {d.day}"


def next_last_tuesday(today: date | None = None) -> date:
    today = today or date.today()
    candidate = last_tuesday(today.year, today.month)
    if candidate >= today:
        return candidate
    year, month = today.year, today.month + 1
    if month == 13:
        year, month = year + 1, 1
    return last_tuesday(year, month)


def _current_members() -> list[dict]:
    return sorted(
        [m for m in corpus_read.members() if m.get("isCurrent")],
        key=lambda m: m.get("name") or m.get("slug") or "",
    )


def next_meeting() -> dict:
    upcoming = corpus_read.upcoming_meetings()
    book = None
    if upcoming:
        meeting_book = corpus_read.find_book(upcoming[0]["title"])
        if meeting_book:
            book = meeting_book

    inferred_date = next_last_tuesday().isoformat()
    meeting_date = (book or {}).get("meetingDate") or inferred_date
    book_slug = (book or {}).get("slug")
    meeting_key = book_slug or meeting_date[:10]
    picker_slugs = [s for s in ((book or {}).get("pickerSlugs") or []) if s]
    picker_names = (book or {}).get("pickerNames") or []
    meeting_id = clubdb.meeting_id_for_book_slug(book_slug)
    return {
        "meetingKey": meeting_key,
        "meetingId": meeting_id,
        "pickerIds": clubdb.picker_ids_for_book_slug(book_slug),
        "date": meeting_date[:10],
        "startTime": clubdb.start_time_for_meeting(meeting_id),
        "expectedRuleDate": last_tuesday(
            int(meeting_date[:4]), int(meeting_date[5:7])
        ).isoformat() if meeting_date else inferred_date,
        "book": {
            "slug": (book or {}).get("slug"),
            "title": (book or {}).get("title"),
            "authors": (book or {}).get("authors") or [],
        } if book else None,
        "pickerSlugs": picker_slugs,
        "pickerNames": picker_names,
    }


def meeting_status(meeting_id: int | None = None) -> dict:
    meeting = next_meeting()
    mid = meeting_id if meeting_id is not None else meeting["meetingId"]
    roll_call = db.current_roll_call(mid) if mid is not None else None
    status_rows = {
        r["member_id"]: r
        for r in (db.meeting_member_status_for_meeting(mid) if mid is not None else [])
    }
    members = clubdb.current_members()
    picker_ids = set(meeting.get("pickerIds") or [])

    rows = []
    yes = no = unsure = 0
    for member in members:
        row = status_rows.get(member["id"])
        # a missing row, or attendance still 'unknown', counts as pending
        status = row["attendance"] if row and row["attendance"] != "unknown" else "pending"
        if status == "yes":
            yes += 1
        elif status == "no":
            no += 1
        elif status == "unsure":
            unsure += 1
        rows.append({
            "member": member.get("name"),
            "memberId": member["id"],
            "memberSlug": member["slug"],
            "status": status,
            "isPicker": member["id"] in picker_ids,
            "updatedAt": row.get("attendance_answered_at") if row else None,
        })

    pending = len([r for r in rows if r["status"] == "pending"])
    possible_yes = yes + unsure + pending
    picker_rows = [r for r in rows if r["isPicker"]]
    picker_available = bool(picker_rows) and all(r["status"] == "yes" for r in picker_rows)
    picker_declined = any(r["status"] == "no" for r in picker_rows)
    picker_pending = bool(picker_rows) and not picker_available and not picker_declined

    has_quorum = yes >= QUORUM_REQUIRED
    quorum_impossible = possible_yes < QUORUM_REQUIRED
    risks: list[str] = []
    if not has_quorum:
        risks.append("quorum_not_confirmed")
    if quorum_impossible:
        risks.append("quorum_impossible")
    if picker_declined:
        risks.append("picker_unavailable")
    elif picker_pending:
        risks.append("picker_not_confirmed")
    if meeting["date"] != meeting["expectedRuleDate"]:
        risks.append("not_last_tuesday")

    if has_quorum and picker_available:
        recommendation = "ready"
    elif quorum_impossible or picker_declined:
        recommendation = "needs_attention"
    else:
        recommendation = "waiting"

    return {
        "meeting": meeting,
        "rollCall": roll_call,
        "attendance": rows,
        "counts": {
            "yes": yes,
            "no": no,
            "unsure": unsure,
            "pending": pending,
            "currentMembers": len(members),
            "quorumRequired": QUORUM_REQUIRED,
        },
        "rules": {
            "standingDate": "last Tuesday of the month",
            "quorum": f"{QUORUM_REQUIRED} of {len(members)} current members",
            "pickerMustAttend": True,
        },
        "hasQuorum": has_quorum,
        "pickerAvailable": picker_available,
        "risks": risks,
        "recommendation": recommendation,
    }


def format_status(status: dict) -> str:
    meeting = status["meeting"]
    title = (meeting.get("book") or {}).get("title") or "the next meeting"
    counts = status["counts"]
    lines = [
        f"Roll call for **{title}** on {meeting['date']}: "
        f"{counts['yes']} yes, {counts['no']} no, {counts['unsure']} unsure, "
        f"{counts['pending']} pending.",
    ]
    if status["hasQuorum"]:
        lines.append("Quorum is confirmed.")
    else:
        lines.append(f"Quorum is not confirmed yet; we need {counts['quorumRequired']} yes responses.")
    if meeting.get("pickerNames"):
        picker = ", ".join(meeting["pickerNames"])
        if status["pickerAvailable"]:
            lines.append(f"The picker ({picker}) is confirmed attending.")
        elif "picker_unavailable" in status["risks"]:
            lines.append(f"The picker ({picker}) cannot attend, so reading order may need attention.")
        else:
            lines.append(f"The picker ({picker}) is not confirmed yet.")
    if "not_last_tuesday" in status["risks"]:
        lines.append(
            f"Note: the corpus date is {meeting['date']}, but the last Tuesday is "
            f"{meeting['expectedRuleDate']}."
        )
    return "\n".join(lines)


def summarize_club_state() -> dict:
    current = _current_members()
    identities = db.list_member_identities()
    linked = {r["member_slug"] for r in identities}
    recent = corpus_read.club_stats()
    return {
        "members": [
            {
                "slug": m.get("slug"),
                "name": m.get("name"),
                "discordLinked": m.get("slug") in linked,
            }
            for m in current
        ],
        "nextMeeting": meeting_status(),
        "stats": {
            "totalRead": recent.get("totalRead"),
            "fiction": recent.get("fiction"),
            "nonfiction": recent.get("nonfiction"),
            "lastYear": recent.get("lastYear"),
        },
        "feedback": db.feedback_stats(),
    }


# ── Roll-call email text ─────────────────────────────────────────────────────
# Shared by both senders — the `/oliver meeting roll-call` command path (commands.py) and the
# request_roll_call_update tool (tools.py) — so subject/body wording can't drift between
# the two copies. Pure text; lives here (not in club/meeting_emails, which imports oliver)
# to stay free of the oliver→tools import cycle.

def days_until_text(meeting_date: str) -> str:
    """Human phrasing for how far off a meeting date is ("today", "in 3 days", …)."""
    try:
        days = (date.fromisoformat(meeting_date) - date.today()).days
    except ValueError:
        return ""
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days > 1:
        return f"in {days} days"
    return f"{abs(days)} days ago"


def roll_call_subject(status: dict) -> str:
    meeting = status["meeting"]
    title = (meeting.get("book") or {}).get("title") or "the next meeting"
    return f"Roll call: {title} on {meeting['date']}"


def roll_call_email_body(member_name: str, status: dict, *, note: str | None = None) -> str:
    """Plain-text roll-call email asking one member whether they can attend.

    `note` appends an optional extra line (used by the tool path); the command path
    passes none, producing byte-identical output to the old local copies.
    """
    meeting = status["meeting"]
    title = (meeting.get("book") or {}).get("title") or "the next meeting"
    timing = days_until_text(meeting["date"])
    meeting_when = f"{meeting['date']}" + (f" ({timing})" if timing else "")
    picker = ", ".join(meeting.get("pickerNames") or [])
    picker_line = f"\n\n{picker} picked this one, and the picker needs to be able to attend." if picker else ""
    extra = f"\n\n{note.strip()}" if note else ""
    counts = status["counts"]
    return (
        f"Hi {member_name},\n\n"
        f"Roll call for {title}: the meeting is {meeting_when}.\n\n"
        "Can you make it? Reply with yes, no, or unsure and I'll update the roll-call tracker."
        f"{picker_line}"
        f"{extra}\n\n"
        f"Current status: {counts['yes']} yes, {counts['no']} no, "
        f"{counts['unsure']} unsure, {counts['pending']} pending. "
        f"We need {counts['quorumRequired']} yes responses."
    )
