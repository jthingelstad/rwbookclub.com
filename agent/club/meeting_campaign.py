"""Meeting readiness campaign state for Oliver.

This module is the single place that combines attendance, reading progress,
member identity/contact state, and club meeting rules into "what should happen
next". Commands and tools should read this snapshot instead of re-deriving their
own partial versions.
"""

from __future__ import annotations

from datetime import date

from agent import corpus_read, db
from agent.club import meeting_rules

READING_OK = {"finished", "on_track"}
ROLL_CALL_START_DAYS = 14
MAX_READING_CHECKINS = 3
READING_CHECKIN_THRESHOLDS = (14, 7, 2)
MIN_DAYS_BETWEEN_READING_CHECKINS = 2


def snapshot() -> dict:
    meeting_status = meeting_rules.meeting_status()
    meeting = meeting_status["meeting"]
    meeting_id = meeting["meetingId"]
    status_rows = {
        r["member_id"]: r
        for r in (db.meeting_member_status_for_meeting(meeting_id) if meeting_id is not None else [])
    }
    discord_linked = {r["member_slug"] for r in db.list_member_identities()}
    email_linked = {r["member_slug"] for r in db.list_member_emails()}
    members_by_slug = {
        m["slug"]: m for m in corpus_read.members() if m.get("isCurrent")
    }
    needs_roll_call = []
    needs_reading = []
    member_rows = []
    attending_reading_ok = 0
    attending_reading_not_ok = 0

    for row in meeting_status["attendance"]:
        slug = row["memberSlug"]
        mid = row["memberId"]
        member = members_by_slug.get(slug, {"name": row["member"], "slug": slug})
        srow = status_rows.get(mid)
        reading_status = srow["reading"] if srow else "unknown"
        reading_ok = reading_status in READING_OK
        combined = {
            "member": member.get("name"),
            "memberSlug": slug,
            "memberId": mid,
            "attendance": row["status"],
            "reading": reading_status,
            "readingOk": reading_ok,
            "readingProgress": srow.get("reading_progress") if srow else None,
            "page": srow.get("reading_page") if srow else None,
            "percent": srow.get("reading_percent") if srow else None,
            "isPicker": row["isPicker"],
            "discordLinked": slug in discord_linked,
            "emailLinked": slug in email_linked,
            "lastAskedAt": srow.get("last_asked_at") if srow else None,
            "readingLastAskedAt": srow.get("reading_last_asked_at") if srow else None,
            "readingCheckinCount": (srow.get("reading_asks") if srow else 0) or 0,
        }
        if row["status"] == "pending":
            combined["nextAction"] = "roll_call"
            needs_roll_call.append(combined)
        elif row["status"] == "yes":
            if reading_ok:
                combined["nextAction"] = "none"
                attending_reading_ok += 1
            else:
                combined["nextAction"] = "reading_checkin"
                attending_reading_not_ok += 1
                needs_reading.append(combined)
        elif row["status"] == "unsure":
            combined["nextAction"] = "confirm_attendance"
        else:
            combined["nextAction"] = "none"
        member_rows.append(combined)

    ready = meeting_status["recommendation"] == "ready" and not needs_reading
    actions = _recommended_actions(
        meeting_status=meeting_status,
        needs_roll_call=needs_roll_call,
        needs_reading=needs_reading,
        ready=ready,
    )
    return {
        "meeting": meeting,
        "book": meeting.get("book"),
        "daysUntilMeeting": _days_until(meeting["date"]),
        "members": member_rows,
        "attendance": meeting_status,
        "ready": ready,
        "counts": {
            "attending": meeting_status["counts"]["yes"],
            "quorumRequired": meeting_status["counts"]["quorumRequired"],
            "attendingReadingOk": attending_reading_ok,
            "attendingReadingNotOk": attending_reading_not_ok,
            "needsRollCall": len(needs_roll_call),
            "needsReading": len(needs_reading),
        },
        "needsRollCall": needs_roll_call,
        "needsReading": needs_reading,
        "recommendedActions": actions,
    }


def format_dashboard(data: dict | None = None) -> str:
    data = data or snapshot()
    meeting = data["meeting"]
    title = (meeting.get("book") or {}).get("title") or "the next meeting"
    days = data.get("daysUntilMeeting")
    days_text = _days_text(days)
    counts = data["counts"]
    status = data["attendance"]
    lines = [
        f"**Meeting dashboard:** {title} on {meeting['date']} ({days_text})",
        f"Attendance: {counts['attending']} yes / {counts['quorumRequired']} needed; "
        f"picker {'confirmed' if status['pickerAvailable'] else 'not confirmed'}.",
        f"Reading: {counts['attendingReadingOk']} attending on track/finished; "
        f"{counts['attendingReadingNotOk']} attending need reading check-in.",
    ]
    if data["ready"]:
        lines.append("Status: ready.")
    else:
        lines.append("Status: not ready.")

    if data["recommendedActions"]:
        lines.append("\n**Next actions:**")
        for action in data["recommendedActions"][:5]:
            lines.append(f"• {action['label']}")

    lines.append("\n**Members:**")
    for member in data["members"]:
        last = member.get("lastAskedAt")
        contact_text = f"; last asked {str(last)[:10]}" if last else ""
        reading = member["reading"].replace("_", " ")
        lines.append(
            f"• {member['member']}: attendance {member['attendance']}; "
            f"reading {reading}; next {member['nextAction']}{contact_text}"
        )
    return "\n".join(lines)


def reading_checkin_candidates(data: dict | None = None, *, today: date | None = None) -> list[dict]:
    """Confirmed attendees due for a reading nudge under the campaign rules.

    Rule: after a member confirms attendance, Oliver may ask for reading status
    no more than three times before the meeting. The three windows are: first
    eligible at 14 days, second at 7 days, final at 2 days, with at least two
    days between automated asks.
    """
    data = data or snapshot()
    today = today or date.today()
    days = data.get("daysUntilMeeting")
    if days is None or days < 0 or days > ROLL_CALL_START_DAYS:
        return []
    candidates = []
    for member in data["needsReading"]:
        count = int(member.get("readingCheckinCount") or 0)
        if count >= MAX_READING_CHECKINS:
            continue
        threshold = READING_CHECKIN_THRESHOLDS[count]
        if days > threshold:
            continue
        if count > 0:
            age = _age_days(member.get("readingLastAskedAt"), today=today)
            if age is not None and age < MIN_DAYS_BETWEEN_READING_CHECKINS:
                continue
        candidates.append({
            **member,
            "checkinNumber": count + 1,
            "maxCheckins": MAX_READING_CHECKINS,
            "reason": f"reading check-in {count + 1} of {MAX_READING_CHECKINS}",
        })
    return candidates


def _recommended_actions(*, meeting_status: dict, needs_roll_call: list[dict],
                         needs_reading: list[dict], ready: bool) -> list[dict]:
    if ready:
        return [{"kind": "ready", "label": "No action needed; quorum, picker, and reading status are in shape."}]
    actions = []
    if "quorum_impossible" in meeting_status["risks"]:
        actions.append({
            "kind": "admin_attention",
            "label": "Quorum is impossible from current replies; humans need to decide whether to adjust.",
        })
    elif not meeting_status["hasQuorum"] and needs_roll_call:
        names = ", ".join(m["member"] for m in needs_roll_call)
        actions.append({
            "kind": "roll_call",
            "label": f"Ask pending members for roll call: {names}.",
            "members": [m["memberSlug"] for m in needs_roll_call],
        })

    if "picker_unavailable" in meeting_status["risks"]:
        actions.append({
            "kind": "admin_attention",
            "label": "The picker cannot attend; humans need to decide how to handle the meeting.",
        })
    elif not meeting_status["pickerAvailable"]:
        picker_slugs = set(meeting_status["meeting"].get("pickerSlugs") or [])
        pickers = [m for m in meeting_status["attendance"] if m["memberSlug"] in picker_slugs]
        names = ", ".join(m["member"] for m in pickers) or "the picker"
        actions.append({
            "kind": "picker_roll_call",
            "label": f"Confirm picker attendance with {names}.",
            "members": [m["memberSlug"] for m in pickers],
        })

    if needs_reading:
        names = ", ".join(m["member"] for m in needs_reading)
        actions.append({
            "kind": "reading_checkin",
            "label": f"Ask attending members for reading progress: {names}.",
            "members": [m["memberSlug"] for m in needs_reading],
        })
    return actions


def _age_days(timestamp: str | None, *, today: date) -> int | None:
    if not timestamp:
        return None
    try:
        when = date.fromisoformat(str(timestamp)[:10])
    except ValueError:
        return None
    return (today - when).days


def _days_until(meeting_date: str) -> int | None:
    try:
        return (date.fromisoformat(meeting_date) - date.today()).days
    except ValueError:
        return None


def _days_text(days: int | None) -> str:
    if days is None:
        return "date unknown"
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days > 1:
        return f"in {days} days"
    return f"{abs(days)} days ago"
