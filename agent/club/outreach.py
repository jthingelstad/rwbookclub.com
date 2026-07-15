"""Member-scoped meeting outreach composition and delivery."""

from __future__ import annotations

import asyncio
import logging

from agent import clock, clubdb, db, identities, oliver
from agent import corpus_read
from agent.club import meeting_campaign, meeting_rules
from agent.mail import outbound

log = logging.getLogger("oliver.outreach")


async def send_roll_call_email(
    member: dict, status: dict, *, idempotency_key: str | None = None
) -> dict | None:
    email = identities.email_for_member(member["slug"])
    if not email:
        return None
    meeting = status["meeting"]
    title = (meeting.get("book") or {}).get("title") or "the next meeting"
    subject = meeting_rules.roll_call_subject(status)
    timing = meeting_rules.days_until_text(meeting["date"])
    counts = status["counts"]
    picker = ", ".join(meeting.get("pickerNames") or [])
    body = await asyncio.to_thread(
        oliver.compose,
        "roll-call email asking a club member whether they can attend the next meeting",
        {
            "recipient name": member["name"],
            "book": title,
            "meeting date": meeting["date"] + (f" ({timing})" if timing else ""),
            "picker": picker or None,
            "picker rule": "the picker needs to be able to attend" if picker else None,
            "how to respond": "reply yes, no, or unsure and Oliver updates the roll-call tracker",
            "current responses": (
                f"{counts['yes']} yes, {counts['no']} no, {counts['unsure']} unsure, "
                f"{counts['pending']} pending; {counts['quorumRequired']} yes responses needed"
            ),
        },
        fallback=meeting_rules.roll_call_email_body(member["name"], status),
        medium="email",
    )
    meeting_id = meeting["meetingId"]
    member_id = clubdb.lookup_member_id(member["slug"])
    if meeting_id is None or member_id is None:
        return None
    sent_email = await asyncio.to_thread(
        outbound.send,
        to=[email["email"]],
        subject=subject,
        body=body,
        idempotency_key=idempotency_key,
        policy="linked_member",
    )
    await asyncio.to_thread(
        db.record_attendance_request,
        meeting_id,
        member_id,
        actor="oliver",
        surface="email",
    )
    db.add_activity(
        "email_sent",
        "Roll-call email sent",
        f"Member: {member['slug']}\nTo: {email['email']}\nSubject: {subject}",
    )
    return sent_email


async def send_reading_checkin_email(
    member: dict,
    meeting: dict,
    *,
    note: str | None = None,
    idempotency_key: str | None = None,
) -> dict | None:
    email = identities.email_for_member(member["slug"])
    if not email:
        return None
    title = (meeting.get("book") or {}).get("title") or "the current book"
    subject = f"Reading check-in: {title}"
    timing = meeting_rules.days_until_text(meeting["date"])
    body = await asyncio.to_thread(
        oliver.compose,
        "short reading check-in email asking a club member how far along they are in the book",
        {
            "recipient name": member["name"],
            "book": title,
            "meeting date": meeting["date"] + (f" ({timing})" if timing else ""),
            "how to respond": (
                'reply briefly like "halfway and on track", "page 120, behind", or "finished" '
                "and Oliver updates the tracker"
            ),
            "extra note": note,
        },
        fallback=meeting_rules.reading_checkin_email_body(
            member["name"], meeting, note=note
        ),
        medium="email",
    )
    meeting_id = meeting["meetingId"]
    member_id = clubdb.lookup_member_id(member["slug"])
    if meeting_id is None or member_id is None:
        return None
    sent = await asyncio.to_thread(
        outbound.send,
        to=[email["email"]],
        subject=subject,
        body=body,
        idempotency_key=idempotency_key,
        policy="linked_member",
    )
    await asyncio.to_thread(
        db.record_reading_request,
        meeting_id,
        member_id,
        actor="oliver",
        surface="email",
    )
    db.add_activity(
        "email_sent",
        "Reading check-in email sent",
        f"Member: {member['slug']}\nTo: {email['email']}\nSubject: {subject}\nEmail ID: {sent.get('emailId')}",
    )
    return sent


# Oliver runs meeting prep once a day, at this local hour, so the per-member evaluation (and its
# decision calls) happen on a daily cadence rather than on every hourly scheduler tick.
MEETING_OUTREACH_HOUR = 9  # America/Chicago


async def run(meeting: dict, status: dict) -> int:
    """Autonomous per-member meeting prep: roll call until attendance is answered, then reading
    check-ins until finished — email only, no admin needed.

    `meeting_campaign.outreach_plan` applies the hard rails (2-week window, the 3-day floor, and the
    ceiling/kickoff that sets `mustReach`); for the discretionary middle cases Oliver decides via
    `oliver.decide_outreach`. Reuses the existing per-member senders, which compose the email and
    record the `attendance_request` / `reading_request` event. Returns the number of emails sent.
    """
    campaign = await asyncio.to_thread(meeting_campaign.snapshot)
    plan = meeting_campaign.outreach_plan(campaign, today=clock.club_now().date())
    posted = 0
    for cand in plan:
        slug = cand["memberSlug"]
        member = corpus_read.find_member(slug)
        if not member or not identities.email_for_member(slug):
            continue
        reach = cand["mustReach"] or await asyncio.to_thread(
            oliver.decide_outreach, cand
        )
        if not reach:
            continue
        try:
            if cand["kind"] == "attendance":
                sent = await send_roll_call_email(
                    member,
                    status,
                    idempotency_key=(
                        f"email:meeting-outreach:{meeting['meetingId']}:attendance:"
                        f"{slug}:{cand['asksSoFar']}"
                    ),
                )
            else:
                sent = await send_reading_checkin_email(
                    member,
                    meeting,
                    note="Automated reading check-in.",
                    idempotency_key=(
                        f"email:meeting-outreach:{meeting['meetingId']}:reading:"
                        f"{slug}:{cand['asksSoFar']}"
                    ),
                )
        except Exception:
            log.exception("meeting outreach (%s) failed for %s", cand["kind"], slug)
            continue
        if sent:
            db.add_activity(
                "meeting_outreach",
                f"Meeting {cand['kind']} outreach sent",
                f"Member: {slug}\nKind: {cand['kind']}\n"
                f"Reason: {'forced' if cand['mustReach'] else 'Oliver chose to reach out'}\n"
                f"Meeting: {meeting['meetingKey']}",
            )
            posted += 1
    return posted
