"""Inbound email orchestration with explicit, independently testable stages."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable

from agent import clubdb, config, db, oliver
from agent.club import meeting_rules, review_drive
from agent.mail import email_jmap, email_policy, mail_archive, outbound

log = logging.getLogger("oliver")

ROLL_CALL_SUBJECT_RE = re.compile(r"\broll[- ]?call\b", re.IGNORECASE)
EMAIL_QUOTE_RE = re.compile(r"^(>|on .+wrote:|from:|sent:|to:|subject:|--\s*$)", re.IGNORECASE)
YES_RE = re.compile(
    r"\b(yes|yep|yeah|sure|attending|i'?ll be there|i can make it|can make it)\b",
    re.IGNORECASE,
)
NO_RE = re.compile(
    r"\b(no|nope|cannot make it|can'?t make it|won'?t make it|not attending|unavailable)\b",
    re.IGNORECASE,
)
UNSURE_RE = re.compile(r"\b(unsure|not sure|maybe|tentative|unknown)\b", re.IGNORECASE)


def record_ignored_email(msg: email_jmap.InboundEmail, reason: str) -> None:
    body = (
        f"From: {msg.speaker} <{msg.from_email}>\n"
        f"Subject: {msg.subject or '(no subject)'}\nReason: {reason}"
    )
    db.add_activity("email_ignored", "Email ignored", body)
    log.info("Ignored email %s from %s: %s", msg.id, msg.from_email, reason)


def _first_reply_text(text: str) -> str:
    """Return the member-authored top of an email, excluding quoted history."""
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if lines:
                break
            continue
        if EMAIL_QUOTE_RE.match(line):
            break
        lines.append(line)
        if len(lines) >= 3:
            break
    return " ".join(lines)


def roll_call_status_from_email(subject: str, text: str) -> str | None:
    """Parse explicit roll-call replies before the model sees the email."""
    if not ROLL_CALL_SUBJECT_RE.search(subject or ""):
        return None
    reply = _first_reply_text(text).replace("’", "'")
    if not reply:
        return None
    if UNSURE_RE.search(reply):
        return "unsure"
    if NO_RE.search(reply):
        return "no"
    if YES_RE.search(reply):
        return "yes"
    return None


async def _ignore(
    msg: email_jmap.InboundEmail, reason: str, *, persist_reason: bool = True
) -> None:
    await asyncio.to_thread(email_jmap.mark_seen, msg.id)
    db.mark_email_processing(
        email_id=msg.id,
        thread_id=msg.thread_id,
        from_email=msg.from_email,
        subject=msg.subject,
        received_at=msg.received_at,
    )
    db.mark_email_processed(msg.id, status="ignored", error=reason if persist_reason else None)
    record_ignored_email(msg, reason)


async def _archive(msg: email_jmap.InboundEmail, decision) -> tuple[bool, bool]:
    try:
        archived_new = await asyncio.to_thread(
            mail_archive.archive_inbound_email,
            msg,
            is_mailing_list=decision.is_mailing_list,
            member_slug=decision.member_slug,
        )
    except Exception as exc:
        db.mark_email_processed(
            msg.id, status="failed", error=f"archive:{type(exc).__name__}: {exc}"
        )
        log.exception("Failed to archive inbound email %s", msg.id)
        return False, False
    if archived_new:
        db.add_activity(
            "email_received",
            "Email received",
            f"From: {msg.speaker} <{msg.from_email}>\nSubject: {msg.subject or '(no subject)'}",
        )
    return True, archived_new


def _record_member_reply(msg: email_jmap.InboundEmail, decision, archived_new: bool):
    member_slug = decision.member_slug
    member_id = clubdb.lookup_member_id(member_slug)
    meeting = meeting_rules.next_meeting() if member_slug and member_id is not None else None
    meeting_id = meeting["meetingId"] if meeting else None
    if archived_new and member_slug and member_id is not None:
        db.record_event(
            actor="member",
            kind="email_reply",
            member_id=member_id,
            meeting_id=meeting_id,
            surface="email",
            detail=msg.subject or None,
            source=f"email:{msg.id}",
        )
    recorded_availability = None
    if member_slug and member_id is not None and meeting_id is not None:
        recorded_availability = roll_call_status_from_email(msg.subject, msg.text)
        if recorded_availability:
            db.record_attendance_report(
                meeting_id,
                member_id,
                recorded_availability,
                surface="email",
                updated_by=f"email:{msg.from_email.lower()}",
            )
            db.add_activity(
                "roll_call_update",
                "Roll-call response recorded",
                f"Member: {member_slug}\nStatus: {recorded_availability}\n"
                f"Source: email reply\nMeeting: {meeting['meetingKey']}",
            )
    return member_slug, member_id, recorded_availability


async def _handle_review_reply(
    msg: email_jmap.InboundEmail,
    decision,
    member_id: int | None,
    schedule_publish: Callable[[], None],
) -> bool:
    if decision.is_mailing_list or member_id is None:
        return False
    review_draft = db.draft_for_thread(msg.thread_id)
    if not review_draft or review_draft["member_id"] != member_id:
        return False
    try:
        publish_needed = await asyncio.to_thread(review_drive.handle_reply, review_draft, msg)
    except Exception as exc:
        db.mark_email_processed(
            msg.id, status="failed", error=f"review_drive:{type(exc).__name__}: {exc}"
        )
        log.exception("review-drive reply handling failed for %s", msg.id)
        return True

    # Commit successful state before fallible mailbox/publish bookkeeping. This
    # prevents a completed review reply from being claimed again on the next poll.
    db.mark_email_processed(msg.id)
    try:
        await asyncio.to_thread(email_jmap.mark_seen, msg.id, answered=True)
    except Exception:
        log.exception("Handled review email %s but failed to mark it seen", msg.id)
    if publish_needed:
        try:
            schedule_publish()
        except Exception:
            log.exception("Review %s was recorded but publish scheduling failed", msg.id)
            db.add_activity(
                "warning",
                "Review site publish was not scheduled",
                f"Inbound email {msg.id} completed; run `python -m agent.publish`.",
            )
    return True


async def _mailing_list_result(msg: email_jmap.InboundEmail, decision, member_slug: str | None):
    if not decision.is_mailing_list:
        return True, None
    channel_id = f"email:list:{msg.thread_id or config.BOOK_CLUB_MAILING_LIST_ADDRESS.lower()}"
    speaker_user_id = f"member:{member_slug}" if member_slug else f"email:{msg.from_email.lower()}"
    try:
        result = await asyncio.to_thread(
            oliver.answer_mailing_list_email,
            msg,
            channel_id=channel_id,
            speaker=msg.speaker,
            speaker_user_id=speaker_user_id,
            source_message_id=msg.id,
        )
    except Exception as exc:
        db.mark_email_processed(msg.id, status="failed", error=f"{type(exc).__name__}: {exc}")
        log.exception("Failed to decide whether to reply to mailing-list email %s", msg.id)
        return False, None
    if result.reply:
        return True, result
    await asyncio.to_thread(email_jmap.mark_seen, msg.id)
    reason = f"mailing_list_model_no_reply:{result.reason or 'no_reason'}"
    db.mark_email_processed(msg.id, status="ignored", error=reason)
    record_ignored_email(msg, reason)
    return False, None


async def _compose_reply(
    msg: email_jmap.InboundEmail,
    member_slug: str | None,
    recorded_availability: str | None,
    mailing_list_result,
) -> str | None:
    channel_id = (
        f"email:list:{msg.thread_id or config.BOOK_CLUB_MAILING_LIST_ADDRESS.lower()}"
        if mailing_list_result is not None
        else f"email:{msg.thread_id or msg.from_email.lower()}"
    )
    runtime_note = ""
    if recorded_availability:
        runtime_note = (
            "[Oliver runtime note: this explicit roll-call reply has already "
            f"been recorded as {recorded_availability} for {member_slug}. "
            "Acknowledge the saved status; do not call record_availability again.]\n\n"
        )
    prompt = runtime_note + (
        f"[Email from {msg.speaker} <{msg.from_email}>]\n"
        f"Subject: {msg.subject or '(no subject)'}\n\n{msg.text}"
    )
    try:
        if mailing_list_result is not None:
            return mailing_list_result.body
        return await asyncio.to_thread(
            oliver.answer,
            prompt,
            channel_id,
            msg.speaker,
            f"email:{msg.from_email.lower()}",
            msg.id,
            medium="email",
            max_tokens=oliver.EMAIL_MAX_TOKENS,
        )
    except Exception as exc:
        db.mark_email_processed(
            msg.id, status="failed", error=f"answer:{type(exc).__name__}: {exc}"
        )
        log.exception("Failed to compose reply to inbound email %s", msg.id)
        return None


async def _deliver_reply(msg: email_jmap.InboundEmail, decision, reply: str) -> None:
    recipients = decision.reply_to or [msg.from_email]
    try:
        sent = await asyncio.to_thread(
            outbound.send,
            to=recipients,
            subject=msg.reply_subject,
            body=reply,
            in_reply_to=msg.message_id,
            references=msg.references,
            idempotency_key=f"email:inbound-reply:{msg.id}",
            policy="reply",
        )
    except Exception as exc:
        db.mark_email_processed(msg.id, status="failed", error=f"send:{type(exc).__name__}: {exc}")
        log.exception("Failed to send reply to inbound email %s", msg.id)
        return

    # Provider confirmation is the irreversible boundary. Persist it before any
    # JMAP/archive/activity work so post-send failures cannot trigger a duplicate.
    db.mark_email_processed(msg.id, reply_email_id=sent.get("emailId"))
    try:
        await asyncio.to_thread(email_jmap.mark_seen, msg.id, answered=True)
        await asyncio.to_thread(
            mail_archive.archive_outbound_email,
            msg,
            body=reply,
            to_emails=recipients,
            subject=msg.reply_subject,
            member_slug=decision.member_slug,
            is_mailing_list=decision.is_mailing_list,
            sent_email_id=sent.get("emailId"),
        )
        db.add_activity(
            "email_sent",
            "Email reply sent",
            f"To: {msg.from_email}\nSubject: {msg.reply_subject}\nEmail ID: {sent.get('emailId')}",
        )
    except Exception:
        log.exception("Replied to email %s but post-send bookkeeping failed", msg.id)
    log.info("Replied to email %s from %s", msg.id, msg.from_email)


async def handle(msg: email_jmap.InboundEmail, *, schedule_publish: Callable[[], None]) -> None:
    """Process one inbound message while preserving the send/dedupe transaction boundaries."""
    if db.email_processed(msg.id):
        return
    if msg.from_email.lower() == config.OLIVER_EMAIL_ADDRESS.lower():
        await _ignore(msg, "from_oliver", persist_reason=False)
        return
    decision = email_policy.inbound_decision(msg)
    if not decision.allowed:
        await _ignore(msg, decision.reason)
        return
    if not db.mark_email_processing(
        email_id=msg.id,
        thread_id=msg.thread_id,
        from_email=msg.from_email,
        subject=msg.subject,
        received_at=msg.received_at,
    ):
        return
    archived, archived_new = await _archive(msg, decision)
    if not archived:
        return
    member_slug, member_id, availability = _record_member_reply(msg, decision, archived_new)
    if await _handle_review_reply(msg, decision, member_id, schedule_publish):
        return
    should_reply, mailing_list_result = await _mailing_list_result(msg, decision, member_slug)
    if not should_reply:
        return
    reply = await _compose_reply(msg, member_slug, availability, mailing_list_result)
    if reply is not None:
        await _deliver_reply(msg, decision, reply)
