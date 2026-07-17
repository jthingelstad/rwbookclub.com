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

from agent import clock, clubdb, config, corpus_read, db, identities

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


def friendly_time(hhmm: str | None) -> str:
    """Local 'HH:MM' → '6:30 PM'; '' when the time is unset/unparseable."""
    try:
        h, m = int(str(hhmm)[:2]), int(str(hhmm)[3:5])
    except ValueError, IndexError, TypeError:
        return ""
    return f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def friendly_when(iso: str | None, start_time: str | None = None) -> str:
    """'Tuesday, July 28 at 6:30 PM' when the time is known, else just the friendly date."""
    d = friendly_date(iso)
    t = friendly_time(start_time)
    return f"{d} at {t}" if d and t else d


def with_location(when: str, location: str | None) -> str:
    """Append the venue in parens when set: 'Tuesday, July 28 at 6:30 PM (Broder's)'."""
    return f"{when} ({location})" if when and location else when


def next_last_tuesday(today: date | None = None) -> date:
    today = today or clock.club_today()
    candidate = last_tuesday(today.year, today.month)
    if candidate >= today:
        return candidate
    year, month = today.year, today.month + 1
    if month == 13:
        year, month = year + 1, 1
    return last_tuesday(year, month)


def _current_members() -> list[dict]:
    # Meeting mechanics (attendance, readiness) are human-only — Oliver never gets a roll call.
    return sorted(
        corpus_read.human_current_members(),
        key=lambda m: m.get("name") or m.get("slug") or "",
    )


def horizon(depth: int = 5) -> dict:
    """Read-only scheduled-book runway plus fair-recency pickers for open slots.

    Scheduled upcoming books stay authoritative. Empty slots are awareness only: current members
    are ordered by their least-recently scheduled pick, excluding anyone already represented in
    the scheduled portion before cycling into a second round for depths beyond membership size.
    """
    depth = max(1, min(int(depth), 8))
    current = _current_members()
    members_by_slug = {member["slug"]: member for member in current if member.get("slug")}
    last_scheduled = {slug: None for slug in members_by_slug}
    for book in corpus_read.books():
        meeting_date = book.get("meetingDate")
        if not meeting_date:
            continue
        for slug in book.get("pickerSlugs") or []:
            if slug in last_scheduled and (
                last_scheduled[slug] is None or meeting_date > last_scheduled[slug]
            ):
                last_scheduled[slug] = meeting_date

    ordered = sorted(
        current,
        key=lambda member: (
            last_scheduled.get(member.get("slug")) is not None,
            last_scheduled.get(member.get("slug")) or "",
            (member.get("name") or member.get("slug") or "").lower(),
        ),
    )
    rotation = [
        {
            "slug": member["slug"],
            "name": member.get("name"),
            "lastScheduledPick": last_scheduled.get(member["slug"]),
        }
        for member in ordered
    ]

    slots = []
    scheduled_pickers: set[str] = set()
    for upcoming in corpus_read.upcoming_meetings()[:depth]:
        book = corpus_read.find_book(upcoming.get("slug") or upcoming.get("title")) or {}
        picker_slugs = [slug for slug in (book.get("pickerSlugs") or []) if slug]
        scheduled_pickers.update(slug for slug in picker_slugs if slug in members_by_slug)
        pickers = [
            {"slug": slug, "name": members_by_slug.get(slug, {}).get("name")}
            for slug in picker_slugs
        ]
        placeholder = bool(upcoming.get("placeholder"))
        slots.append(
            {
                "position": len(slots) + 1,
                "status": "scheduled",
                "book": {
                    "slug": upcoming.get("slug"),
                    "title": upcoming.get("title"),
                    "authors": upcoming.get("authors") or [],
                },
                "meetingDate": upcoming.get("meetingDate"),
                "placeholder": placeholder,
                "dateStatus": "soft" if placeholder else "scheduled",
                "picker": pickers[0] if pickers else None,
                "pickers": pickers,
            }
        )

    first_pass = [member for member in ordered if member["slug"] not in scheduled_pickers]
    picker_cycle = first_pass + ordered
    cycle_index = 0
    while len(slots) < depth and picker_cycle:
        if cycle_index >= len(picker_cycle):
            picker_cycle.extend(ordered)
            if not ordered:
                break
        member = picker_cycle[cycle_index]
        cycle_index += 1
        picker = {"slug": member["slug"], "name": member.get("name")}
        slots.append(
            {
                "position": len(slots) + 1,
                "status": "empty",
                "book": None,
                "meetingDate": None,
                "placeholder": False,
                "dateStatus": "open",
                "picker": picker,
                "pickers": [picker],
            }
        )

    empty_slots = [slot for slot in slots if slot["status"] == "empty"]
    return {
        "depth": depth,
        "rule": "open slots use least-recently-scheduled current-member order",
        "rotation": rotation,
        "slots": slots,
        "scheduledCount": len(slots) - len(empty_slots),
        "emptyCount": len(empty_slots),
        "firstEmptyPicker": empty_slots[0]["picker"] if empty_slots else None,
    }


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
        "location": clubdb.location_for_meeting(meeting_id),
        "expectedRuleDate": last_tuesday(int(meeting_date[:4]), int(meeting_date[5:7])).isoformat()
        if meeting_date
        else inferred_date,
        "book": {
            "slug": (book or {}).get("slug"),
            "title": (book or {}).get("title"),
            "authors": (book or {}).get("authors") or [],
        }
        if book
        else None,
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
    members = [
        m for m in clubdb.current_members() if m["slug"] != config.OLIVER_MEMBER_SLUG
    ]  # attendance is human-only
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
        rows.append(
            {
                "member": member.get("name"),
                "memberId": member["id"],
                "memberSlug": member["slug"],
                "status": status,
                "isPicker": member["id"] in picker_ids,
                "updatedAt": row.get("attendance_answered_at") if row else None,
            }
        )

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
    when = with_location(
        friendly_when(meeting["date"], meeting.get("startTime")), meeting.get("location")
    )
    lines = [
        f"Roll call for **{title}** on {when}: "
        f"{counts['yes']} yes, {counts['no']} no, {counts['unsure']} unsure, "
        f"{counts['pending']} pending.",
    ]
    if status["hasQuorum"]:
        lines.append("Quorum is confirmed.")
    else:
        lines.append(
            f"Quorum is not confirmed yet; we need {counts['quorumRequired']} yes responses."
        )
    if meeting.get("pickerNames"):
        picker = ", ".join(meeting["pickerNames"])
        if status["pickerAvailable"]:
            lines.append(f"The picker ({picker}) is confirmed attending.")
        elif "picker_unavailable" in status["risks"]:
            lines.append(
                f"The picker ({picker}) cannot attend, so reading order may need attention."
            )
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
    identity_rows = identities.list_member_identities()
    linked = {row["member_slug"] for row in identity_rows}
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
        days = (date.fromisoformat(meeting_date) - clock.club_today()).days
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
    return f"Roll call: {title} on {friendly_date(meeting['date'])}"


def roll_call_email_body(member_name: str, status: dict, *, note: str | None = None) -> str:
    """Plain-text roll-call email asking one member whether they can attend.

    `note` appends an optional extra line (used by the tool path); the command path
    passes none, producing byte-identical output to the old local copies.
    """
    meeting = status["meeting"]
    title = (meeting.get("book") or {}).get("title") or "the next meeting"
    meeting_when = friendly_when(meeting["date"], meeting.get("startTime"))
    timing = days_until_text(meeting["date"])
    if timing:
        meeting_when += f" ({timing})"
    if meeting.get("location"):
        meeting_when += f", at {meeting['location']}"
    picker = ", ".join(meeting.get("pickerNames") or [])
    picker_line = (
        f"\n\n{picker} picked this one, and the picker needs to be able to attend."
        if picker
        else ""
    )
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


def reading_checkin_email_body(member_name: str, meeting: dict, *, note: str | None = None) -> str:
    """Plain-text reading check-in asking one member where they are in the book. Shared by the
    command path (commands.py) and the request_reading_update tool (tools.py) so the two copies
    can't drift — same reasoning as roll_call_email_body above. Shows the friendly date + time +
    location like the roll-call email does."""
    title = (meeting.get("book") or {}).get("title") or "the current book"
    meeting_when = friendly_when(meeting["date"], meeting.get("startTime"))
    timing = days_until_text(meeting["date"])
    if timing:
        meeting_when += f" ({timing})"
    if meeting.get("location"):
        meeting_when += f", at {meeting['location']}"
    extra = f"\n\n{note.strip()}" if note else ""
    return (
        f"Hi {member_name},\n\n"
        f"Quick reading check-in for {title}. The meeting is {meeting_when}. "
        "Where are you in the book, and do you feel on track?\n\n"
        'Reply with something short like "halfway and on track", '
        '"page 120, behind", or "finished" and I\'ll update the tracker.'
        f"{extra}"
    )
