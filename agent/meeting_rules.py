"""Rules and status helpers for R/W Book Club meeting attendance.

Oliver is allowed to run roll call and flag rule conflicts, but not to decide
the schedule. The club rule stays explicit here:

- meetings normally land on the last Tuesday of the month
- at least 3 of the 5 current members need to attend
- the book picker must be able to attend
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

from agent import corpus_read, db

QUORUM_REQUIRED = 3
MEETING_WEEKDAY = 1  # Tuesday, where Monday is 0.


def last_tuesday(year: int, month: int) -> date:
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - MEETING_WEEKDAY) % 7)


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
    meeting_key = (book or {}).get("slug") or meeting_date[:10]
    picker_slugs = [s for s in ((book or {}).get("pickerSlugs") or []) if s]
    picker_names = (book or {}).get("pickerNames") or []
    return {
        "meetingKey": meeting_key,
        "date": meeting_date[:10],
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


def meeting_status(meeting_key: str | None = None) -> dict:
    meeting = next_meeting()
    key = meeting_key or meeting["meetingKey"]
    roll_call = db.get_roll_call(key)
    attendance = {r["member_slug"]: r for r in db.attendance_for_meeting(key)}
    members = _current_members()
    picker_slugs = set(meeting.get("pickerSlugs") or [])

    rows = []
    yes = no = unsure = 0
    for member in members:
        slug = member["slug"]
        row = attendance.get(slug)
        status = row["status"] if row else "pending"
        if status == "yes":
            yes += 1
        elif status == "no":
            no += 1
        elif status == "unsure":
            unsure += 1
        rows.append({
            "member": member.get("name"),
            "memberSlug": slug,
            "status": status,
            "isPicker": slug in picker_slugs,
            "updatedAt": row.get("responded_at") if row else None,
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
