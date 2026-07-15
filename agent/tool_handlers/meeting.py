"""Actor-scoped meeting, timeline, attendance, reading, and outreach capabilities."""

from __future__ import annotations

from agent import access, clubdb, config, db
from agent import corpus_read as cr
from agent.club import meeting_campaign, meeting_rules
from agent.mail import email_jmap, outbound
from agent.tool_handlers.context import RequestContext

NAMES = frozenset({
    "horizon", "pending_reviews", "current_club_state", "current_meeting_status", "meeting_readiness",
    "meeting_campaign", "club_timeline", "record_timeline_event", "record_availability",
    "record_reading_status", "reading_status", "request_reading_update",
    "request_roll_call_update",
})


def _member_slug(value: str | None) -> str | None:
    member = cr.find_member(value) if value else None
    return member.get("slug") if member else None


def meeting_status_snapshot(actor: access.Actor, meeting_id: int | None = None) -> dict:
    status = meeting_rules.meeting_status(meeting_id)
    if actor.is_admin:
        return status
    roll_call = status.get("rollCall")
    shared_risks = {"quorum_not_confirmed", "quorum_impossible", "not_last_tuesday"}
    return {
        "meeting": status["meeting"],
        "rollCall": ({"status": roll_call.get("status")} if roll_call else None),
        "attendance": [
            row for row in status["attendance"] if row.get("memberSlug") == actor.member_slug
        ],
        "counts": status["counts"],
        "rules": status["rules"],
        "hasQuorum": status["hasQuorum"],
        "risks": [risk for risk in status["risks"] if risk in shared_risks],
        "recommendation": (
            "ready" if status["hasQuorum"] else
            "needs_attention" if "quorum_impossible" in status["risks"] else
            "waiting"
        ),
    }


def current_club_state_snapshot(actor: access.Actor) -> dict:
    state = meeting_rules.summarize_club_state()
    if actor.is_admin:
        return state
    return {
        "members": [
            {"slug": member.get("slug"), "name": member.get("name")}
            for member in state["members"]
        ],
        "nextMeeting": meeting_status_snapshot(actor),
        "stats": state["stats"],
    }


def reading_status_snapshot(meeting: dict, actor: access.Actor) -> dict:
    meeting_id = meeting.get("meetingId")
    rows = {
        row["member_slug"]: row
        for row in (db.meeting_member_status_for_meeting(meeting_id)
                    if meeting_id is not None else [])
    }
    statuses = []
    for member in sorted(cr.human_current_members(), key=lambda row: row.get("name") or row["slug"]):
        row = rows.get(member["slug"])
        statuses.append({
            "member": member.get("name"),
            "memberSlug": member["slug"],
            "status": row["reading"] if row else "unknown",
            "progress": row.get("reading_progress") if row else None,
            "page": row.get("reading_page") if row else None,
            "percent": row.get("reading_percent") if row else None,
            "updatedAt": row.get("reading_answered_at") if row else None,
        })
    if not actor.is_admin:
        statuses = [row for row in statuses if row["memberSlug"] == actor.member_slug]
    return {"meeting": meeting, "book": meeting.get("book"), "statuses": statuses}


def meeting_readiness_snapshot(actor: access.Actor) -> dict:
    campaign = meeting_campaign.snapshot()
    out = {
        **campaign,
        "reading": reading_status_snapshot(campaign["meeting"], actor),
        "counts": {
            **campaign["counts"],
            "attendingAndFinished": len([
                member for member in campaign["members"]
                if member["attendance"] == "yes" and member["reading"] == "finished"
            ]),
            "attendingNotFinished": len([
                member for member in campaign["members"]
                if member["attendance"] == "yes" and member["reading"] != "finished"
            ]),
        },
    }
    if actor.is_admin:
        return out
    out["members"] = [
        member for member in out["members"] if member["memberSlug"] == actor.member_slug
    ]
    out["needsRollCall"] = [
        member for member in out["needsRollCall"] if member["memberSlug"] == actor.member_slug
    ]
    out["needsReading"] = [
        member for member in out["needsReading"] if member["memberSlug"] == actor.member_slug
    ]
    out["attendance"] = meeting_status_snapshot(
        actor, meeting_id=campaign["meeting"].get("meetingId")
    )
    out["recommendedActions"] = []
    return out


def _event_surface(request: RequestContext) -> str:
    return "email" if request.identity_is_email else "discord"


def _configured_discord_admin(request: RequestContext) -> bool:
    return str(request.speaker_user_id or "") == str(config.ADMIN_USER_ID)


def _pending_reviews(tool_input: dict, request: RequestContext):
    target = _member_slug(tool_input["member"])
    if not target:
        return {"error": "no such member"}
    if not access.can_access_member(request.actor, target):
        return {"error": "you can only inspect your own pending reviews"}
    return cr.pending_reviews(target) or {"error": "no such member"}


def _club_timeline(tool_input: dict, request: RequestContext):
    limit = max(1, min(int(tool_input.get("limit", 30)), 100))
    member_id = None
    if tool_input.get("member"):
        member = cr.find_member(tool_input["member"])
        member_id = clubdb.lookup_member_id(member["slug"]) if member else None
        if member_id is None:
            return {"error": f"no such member: {tool_input['member']}"}
    rows = db.timeline(
        category=tool_input.get("category"),
        member_id=member_id,
        since=tool_input.get("since"),
        until=tool_input.get("until"),
        limit=limit,
    )
    return [
        {"date": (row.get("occurred_at") or "")[:10], "category": row["category"],
         "kind": row["kind"], "member": row.get("member_slug"),
         "detail": (row.get("detail") or "")[:500], "source": row.get("source")}
        for row in rows
    ]


def _record_timeline_event(tool_input: dict, request: RequestContext):
    actor = request.actor
    category = tool_input.get("category")
    kind = tool_input.get("kind")
    if kind not in (db.CHRONICLE_KINDS.get(category) or ()):
        return {"error": f"kind {kind!r} is not valid for category {category!r}; "
                         f"allowed: {db.CHRONICLE_KINDS.get(category)}"}
    member_id = None
    member_slug = None
    if tool_input.get("member"):
        member = cr.find_member(tool_input["member"])
        if not member:
            return {"error": f"no such member: {tool_input['member']}"}
        member_slug = member["slug"]
        if not access.can_access_member(actor, member_slug):
            return {"error": "you cannot record another member's private milestone"}
        member_id = clubdb.lookup_member_id(member_slug)
    event_id = db.record_event(
        actor="oliver",
        surface=_event_surface(request),
        kind=kind,
        category=category,
        member_id=member_id,
        detail={"summary": tool_input.get("summary"),
                "members": [member_slug] if member_slug else []},
        occurred_at=tool_input.get("date"),
    )
    db.add_activity(
        "timeline_event", "Timeline event recorded",
        f"Category: {category}\nKind: {kind}\nDate: {tool_input.get('date')}\n"
        f"Member: {member_slug or '(club-wide)'}\nSummary: {tool_input.get('summary')}",
    )
    return {"saved": True, "id": event_id, "category": category, "kind": kind}


def _record_availability(tool_input: dict, request: RequestContext):
    if not request.member_slug:
        return {"error": "speaker is not linked to a club member"}
    member_id = clubdb.lookup_member_id(request.member_slug)
    status = tool_input["status"]
    meeting = meeting_rules.next_meeting()
    meeting_id = meeting["meetingId"]
    if meeting_id is None or member_id is None:
        return {"error": "no scheduled meeting to record availability against"}
    db.record_attendance_report(
        meeting_id, member_id, status, surface=_event_surface(request),
        updated_by=request.speaker_user_id,
    )
    db.add_activity(
        "roll_call_update", "Roll-call response recorded",
        f"Member: {request.member_slug}\nStatus: {status}\nMeeting: {meeting['meetingKey']}",
    )
    return {"saved": True,
            "meetingStatus": meeting_status_snapshot(request.actor, meeting_id=meeting_id)}


def _record_reading_status(tool_input: dict, request: RequestContext):
    if not request.member_slug:
        return {"error": "speaker is not linked to a club member"}
    member_id = clubdb.lookup_member_id(request.member_slug)
    meeting = meeting_rules.next_meeting()
    meeting_id = meeting["meetingId"]
    if meeting_id is None or member_id is None:
        return {"error": "no scheduled meeting to record reading status against"}
    db.record_reading_report(
        meeting_id, member_id, tool_input["status"],
        progress=tool_input.get("progress"), page=tool_input.get("page"),
        percent=tool_input.get("percent"), surface=_event_surface(request),
        updated_by=request.speaker_user_id,
    )
    db.add_activity(
        "reading_update", "Reading status recorded",
        f"Member: {request.member_slug}\nStatus: {tool_input['status']}\n"
        f"Progress: {tool_input.get('progress') or '-'}\nMeeting: {meeting['meetingKey']}",
    )
    return {"saved": True, "readingStatus": reading_status_snapshot(meeting, request.actor)}


def _request_reading_update(tool_input: dict, request: RequestContext):
    if request.is_email:
        return {"error": "email check-ins cannot be initiated from inbound email"}
    if not email_jmap.enabled():
        return {"error": "email is not configured"}
    member = cr.find_member(tool_input["member"])
    if not member:
        return {"error": "no such member"}
    if request.member_slug != member["slug"] and not _configured_discord_admin(request):
        return {"error": "only an admin can request check-ins for other members"}
    email = db.email_for_member(member["slug"])
    if not email:
        return {"error": f"{member['name']} has no linked email address"}
    meeting = meeting_rules.next_meeting()
    meeting_id = meeting["meetingId"]
    member_id = clubdb.lookup_member_id(member["slug"])
    if meeting_id is None or member_id is None:
        return {"error": "no scheduled meeting to check in against"}
    title = (meeting.get("book") or {}).get("title") or "the current book"
    existing = db.meeting_member_status(meeting_id, member_id)
    if existing and existing["reading"] == "finished":
        db.add_activity(
            "reading_checkin_skipped", "Reading check-in skipped",
            f"Member: {member['slug']}\nReason: already finished\nBook: {title}",
        )
        return {"sent": False, "member": member["slug"],
                "reason": f"{member['name']} is already marked finished for {title}",
                "readingStatus": reading_status_snapshot(meeting, request.actor)}
    body = meeting_rules.reading_checkin_email_body(
        member["name"], meeting, note=tool_input.get("note")
    )
    subject = f"Reading check-in: {title}"
    sent = outbound.send(
        to=[email["email"]], subject=subject, body=body,
        idempotency_key=(
            f"email:reading-tool:{request.source_message_id}:{member['slug']}"
            if request.source_message_id else None
        ),
        policy="linked_member",
    )
    db.record_reading_request(meeting_id, member_id, surface="email")
    db.add_activity(
        "email_sent", "Reading check-in email sent",
        f"Member: {member['slug']}\nTo: {email['email']}\nSubject: {subject}\n"
        f"Email ID: {sent.get('emailId')}",
    )
    return {"sent": True, "member": member["slug"], **sent}


def _roll_call_targets(tool_input: dict, request: RequestContext):
    requested_member = tool_input.get("member")
    if requested_member:
        member = cr.find_member(requested_member)
        if not member:
            return None, {"error": "no such member"}
        if (request.member_slug != member["slug"]
                and not _configured_discord_admin(request)):
            return None, {
                "error": "only an admin can request roll-call emails for other members"
            }
        return [member], None
    if not _configured_discord_admin(request):
        return None, {"error": "only an admin can email roll call to all members"}
    targets = sorted(
        cr.human_current_members(), key=lambda row: row.get("name") or row["slug"]
    )
    return targets, None


def _partition_roll_call_targets(targets: list[dict], attendance: dict[str, str]):
    pending = []
    skipped = []
    for member in targets:
        member_status = attendance.get(member["slug"], "pending")
        if member_status == "pending":
            pending.append(member)
        else:
            skipped.append({"member": member["slug"], "reason": f"already {member_status}"})
    return pending, skipped


def _send_roll_call_targets(
    targets: list[dict], status: dict, tool_input: dict, request: RequestContext
):
    meeting_id = status["meeting"]["meetingId"]
    sent_rows = []
    missing = []
    for member in targets:
        email = db.email_for_member(member["slug"])
        member_id = clubdb.lookup_member_id(member["slug"])
        if not email:
            missing.append({"member": member["slug"], "reason": "no linked email address"})
            continue
        if member_id is None:
            missing.append({"member": member["slug"], "reason": "not in the club database"})
            continue
        subject = meeting_rules.roll_call_subject(status)
        body = meeting_rules.roll_call_email_body(
            member.get("name") or member["slug"], status, note=tool_input.get("note")
        )
        sent = outbound.send(
            to=[email["email"]], subject=subject, body=body,
            idempotency_key=(
                f"email:roll-call-tool:{request.source_message_id}:{member['slug']}"
                if request.source_message_id else None
            ),
            policy="linked_member",
        )
        db.record_attendance_request(meeting_id, member_id, actor="oliver", surface="email")
        db.add_activity(
            "email_sent", "Roll-call email sent",
            f"Member: {member['slug']}\nTo: {email['email']}\nSubject: {subject}\n"
            f"Email ID: {sent.get('emailId')}",
        )
        sent_rows.append({"member": member["slug"], **sent})
    return sent_rows, missing


def _request_roll_call_update(tool_input: dict, request: RequestContext):
    if request.is_email:
        return {"error": "roll-call emails cannot be initiated from inbound email"}
    if not email_jmap.enabled():
        return {"error": "email is not configured"}
    targets, error = _roll_call_targets(tool_input, request)
    if error:
        return error
    status = meeting_rules.meeting_status()
    meeting = status["meeting"]
    meeting_id = meeting["meetingId"]
    if meeting_id is None:
        return {"error": "no scheduled meeting to run roll call against"}
    attendance = {row["memberSlug"]: row["status"] for row in status["attendance"]}
    pending, skipped = _partition_roll_call_targets(targets, attendance)
    sent_rows, missing = _send_roll_call_targets(pending, status, tool_input, request)
    if sent_rows and not db.has_open_roll_call(meeting_id):
        db.record_group_event(
            meeting_id, "roll_call_opened", actor="oliver",
            detail={"channel_id": request.channel_id, "opened_by": "email-tool"},
        )
    return {"sent": sent_rows, "skipped": skipped, "missing": missing,
            "meetingStatus": meeting_status_snapshot(request.actor, meeting_id=meeting_id)}


def _horizon(tool_input: dict, request: RequestContext):
    return meeting_rules.horizon(tool_input.get("depth", 5))


def _current_club_state(tool_input: dict, request: RequestContext):
    return current_club_state_snapshot(request.actor)


def _current_meeting_status(tool_input: dict, request: RequestContext):
    return meeting_status_snapshot(request.actor)


def _meeting_readiness(tool_input: dict, request: RequestContext):
    return meeting_readiness_snapshot(request.actor)


def _meeting_campaign(tool_input: dict, request: RequestContext):
    return meeting_campaign.snapshot()


def _reading_status(tool_input: dict, request: RequestContext):
    return reading_status_snapshot(meeting_rules.next_meeting(), request.actor)


HANDLERS = {
    "horizon": _horizon,
    "pending_reviews": _pending_reviews,
    "current_club_state": _current_club_state,
    "current_meeting_status": _current_meeting_status,
    "meeting_readiness": _meeting_readiness,
    "meeting_campaign": _meeting_campaign,
    "club_timeline": _club_timeline,
    "record_timeline_event": _record_timeline_event,
    "record_availability": _record_availability,
    "record_reading_status": _record_reading_status,
    "reading_status": _reading_status,
    "request_reading_update": _request_reading_update,
    "request_roll_call_update": _request_roll_call_update,
}
if set(HANDLERS) != NAMES:
    raise RuntimeError(
        f"meeting handler registry mismatch: missing={sorted(NAMES - set(HANDLERS))}, "
        f"extra={sorted(set(HANDLERS) - NAMES)}"
    )


def handle(name: str, tool_input: dict, request: RequestContext):
    try:
        handler = HANDLERS[name]
    except KeyError:
        raise KeyError(name) from None
    return handler(tool_input, request)
