"""Meeting readiness campaign state for Oliver.

This module is the single place that combines attendance, reading progress,
member identity/contact state, and club meeting rules into "what should happen
next". Commands and tools should read this snapshot instead of re-deriving their
own partial versions.
"""

from __future__ import annotations

from datetime import date

from agent import clock, corpus_read, db
from agent.club import meeting_rules

# Dashboard display: a confirmed attendee counts as "reading on track" at on_track/finished. The
# autonomous outreach cadence (outreach_plan) is stricter — it only stops at `finished` — so this is
# used for the snapshot/dashboard only, not the cadence.
READING_OK = {"finished", "on_track"}

# Autonomous meeting-prep cadence (outreach_plan): start two weeks out, pace per member off the event
# log. Never reach out more than once every MIN days (the guard rail). Oliver decides each send; the
# only forced one is the first contact (kickoff). Give up on a member who never responds after
# GIVE_UP asks rather than pestering them up to the meeting.
OUTREACH_START_DAYS = 14
MIN_DAYS_BETWEEN_OUTREACH = 3
GIVE_UP_AFTER_ASKS = 3


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
        m["slug"]: m for m in corpus_read.human_current_members()
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
            "attendanceAsks": (srow.get("attendance_asks") if srow else 0) or 0,
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


def _outreach_kind(member: dict) -> str | None:
    """What Oliver still needs to collect from this member, or None if nothing.

    Roll call first (attendance unknown/unsure), then reading until `finished`. A declined member
    ('no') and a confirmed-and-finished member are both done.
    """
    attendance = member.get("attendance")
    if attendance in ("pending", "unsure"):
        return "attendance"
    if attendance == "yes" and member.get("reading") != "finished":
        return "reading"
    return None


def outreach_plan(data: dict | None = None, *, today: date | None = None) -> list[dict]:
    """The per-member meeting-prep outreach that's eligible today.

    Starting OUTREACH_START_DAYS before the meeting, each current member with a linked email and an
    open need (see _outreach_kind) is a candidate once the MIN_DAYS_BETWEEN_OUTREACH floor since their
    last ask has cleared — Oliver never contacts a member more often than that. Oliver decides each
    send (oliver.decide_outreach) EXCEPT the very first contact, which is forced (`mustReach`) so the
    conversation always starts. Members who've never responded to a kind are dropped after
    GIVE_UP_AFTER_ASKS tries — Oliver gives up rather than pestering someone who's gone quiet.
    """
    data = data or snapshot()
    today = today or clock.club_today()
    days = data.get("daysUntilMeeting")
    if days is None or days < 0 or days > OUTREACH_START_DAYS:
        return []
    plan: list[dict] = []
    for member in data["members"]:
        if not member.get("emailLinked"):
            continue  # email-only cadence — unreachable members are skipped
        kind = _outreach_kind(member)
        if kind is None:
            continue
        asks = int((member.get("attendanceAsks") if kind == "attendance"
                    else member.get("readingCheckinCount")) or 0)
        # "Responded to this kind" = they've answered roll call / reported any reading. A member who
        # never responds is given up on after GIVE_UP_AFTER_ASKS, so we don't pester the silent.
        responded = (member.get("attendance") != "pending") if kind == "attendance" \
            else (member.get("reading") != "unknown")
        if not responded and asks >= GIVE_UP_AFTER_ASKS:
            continue
        since = _age_days(member.get("lastAskedAt"), today=today)
        if since is not None and since < MIN_DAYS_BETWEEN_OUTREACH:
            continue  # the guard rail — too soon since the last outreach
        # Force only the first contact (never asked AND never responded) — the kickoff. Everything
        # after that is Oliver's judgment, so an already-engaged member is never force-pinged.
        must_reach = since is None and not responded
        plan.append({
            "memberSlug": member["memberSlug"],
            "memberId": member["memberId"],
            "member": member["member"],
            "kind": kind,
            "attendance": member["attendance"],
            "reading": member["reading"],
            "readingProgress": member.get("readingProgress"),
            "daysSinceLastAsk": since,
            "asksSoFar": asks,
            "daysUntilMeeting": days,
            "mustReach": must_reach,
        })
    return plan


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
        return (date.fromisoformat(meeting_date) - clock.club_today()).days
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
