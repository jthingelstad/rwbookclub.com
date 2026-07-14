"""Actor-scoped mail archive reads and model-initiated outbound email."""

from __future__ import annotations

from agent import access, config, db, model_readers
from agent import corpus_read as cr
from agent.mail import email_jmap, email_policy, outbound
from agent.tool_handlers.context import RequestContext

NAMES = frozenset({"search_mail_archive", "get_mail_thread", "send_email", "email_status"})


def _member_slug(value: str | None) -> str | None:
    member = cr.find_member(value) if value else None
    return member.get("slug") if member else None


def handle(name: str, tool_input: dict, request: RequestContext):
    actor = request.actor
    if name == "search_mail_archive":
        limit = max(1, min(int(tool_input.get("limit", 8)), 20))
        requested = tool_input.get("member")
        target = _member_slug(requested) if requested else None
        if requested and not target:
            return {"error": f"no such member: {requested}"}
        if target and not access.can_access_member(actor, target):
            return {"error": "another member's private email history is unavailable"}
        rows = model_readers.search_mail(
            actor=actor,
            query=tool_input["query"],
            member_slug=target,
            year_from=tool_input.get("year_from"),
            year_to=tool_input.get("year_to"),
            limit=limit,
        )
        for row in rows:
            row["snippet"] = (row.get("snippet") or "")[:500]
        return rows
    if name == "get_mail_thread":
        limit = max(1, min(int(tool_input.get("limit", 50)), 100))
        thread = model_readers.mail_thread(
            actor=actor, thread_id=tool_input["thread_id"], limit=limit
        )
        if not thread:
            return {"error": "no accessible mail thread"}
        for message in thread["messages"]:
            message["body_clean"] = (message.get("body_clean") or "")[:1000]
        return thread
    if name == "send_email":
        if request.is_email:
            return {
                "error": "inbound email replies are sent automatically; write response text instead"
            }
        if not email_jmap.enabled():
            return {"error": "email is not configured"}
        recipient_error = email_policy.validate_model_email_recipients(
            to=tool_input["to"], cc=tool_input.get("cc")
        )
        if recipient_error:
            return {"error": recipient_error}
        result = outbound.send(
            to=tool_input["to"],
            subject=tool_input["subject"],
            body=tool_input["body"],
            cc=tool_input.get("cc"),
            idempotency_key=(
                f"email:model:{request.source_message_id}"
                if request.source_message_id else None
            ),
            policy="model",
        )
        db.add_activity(
            "email_sent",
            "Email sent",
            f"To: {', '.join(result.get('to') or [])}\nSubject: {result.get('subject')}\n"
            f"Email ID: {result.get('emailId')}",
        )
        return {"sent": True, **result}
    if name == "email_status":
        return {
            "configured": email_jmap.enabled(),
            "address": config.OLIVER_EMAIL_ADDRESS,
            "inbox": f"{config.OLIVER_EMAIL_INBOX_PARENT}/{config.OLIVER_EMAIL_INBOX_FOLDER}",
            "sent": f"{config.OLIVER_EMAIL_SENT_PARENT}/{config.OLIVER_EMAIL_SENT_FOLDER}",
        }
    raise KeyError(name)
