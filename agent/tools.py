"""Oliver's tool surface: Anthropic tool schemas + a dispatcher.

Two kinds of tools live here:
- **Server-side** (Anthropic-hosted): `web_search` — the platform resolves it,
  `dispatch()` never sees it because the response blocks have type
  `server_tool_use`, not `tool_use`.
- **Client-side** (this module): corpus reads (find_books, get_book, …) plus
  SQLite writes (remember/recall/set_reminder). All read-only or local-only —
  nothing irreversible. `dispatch` is called by the agent loop with the tool
  name, model's input, and per-turn context; returns a compact JSON string.

Tool errors are caught and returned to the model as `{"error": ...}` so the
loop survives — but also `log.exception` so the operator can see what broke.
"""

from __future__ import annotations

import json
import logging
from datetime import date

from agent import config
from agent import corpus_read as cr
from agent import db
from agent.mail import email_jmap
from agent.mail import email_policy
from agent.mail import outbound
from agent.club import meeting_campaign
from agent.club import meeting_rules

log = logging.getLogger("oliver.tools")

# Tool definitions sent to the API. Order is stable so the prompt-cache prefix
# (tools render before system) stays valid across requests.
TOOLS = [
    # Anthropic server-side web search — handled by the platform, not by dispatch().
    # Use sparingly (see OPERATIONAL_PROMPT): corpus tools first; web only for specific
    # world facts (dates, numbers, names) you'd otherwise guess at.
    {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 3,
    },
    {
        "name": "find_books",
        "description": "BEST FIRST CHOICE for any vague or exploratory question about books "
                       "the club has read ('anything about urban planning?', 'sci-fi we've "
                       "read', 'have we done long history stuff'). One call returns the most "
                       "relevant matches scored across author / topic / title / synopsis. "
                       "Use this instead of running multiple search_books variants. If "
                       "find_books returns [], the corpus genuinely doesn't have anything "
                       "in that lane — don't keep searching; say so plainly.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "free-text — a topic, theme, author name, or phrase"}},
            "required": ["query"],
        },
    },
    {
        "name": "search_books",
        "description": "Precise filter-based browse — use when you want to LIST everything "
                       "matching specific criteria (all 2018 reads, all Technology books, "
                       "all sci-fi). Filters work alone — omit `query` for a pure filter "
                       "browse. For vague \"do we have anything about X\" questions, use "
                       "find_books instead.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "free-text substring match on title/subtitle/synopsis/author/topic. Optional — omit for filter-only browse."},
                "topic": {"type": "string", "description": "exact topic category, e.g. 'Technology'"},
                "fiction": {"type": "boolean"},
                "year": {"type": "integer", "description": "year read or publication year"},
                "author": {"type": "string"},
            },
        },
    },
    {
        "name": "get_book",
        "description": "Full detail on one book the club has read — synopsis, meeting info, and member reviews.",
        "input_schema": {
            "type": "object",
            "properties": {"book": {"type": "string", "description": "book slug or title"}},
            "required": ["book"],
        },
    },
    {
        "name": "related_books",
        "description": "Find books in the club corpus related to one book by author, topic, Open Library subjects, and synopsis language. Use for 'what else is like X?' or thematic bridges.",
        "input_schema": {
            "type": "object",
            "properties": {
                "book": {"type": "string", "description": "book slug or title"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 12},
            },
            "required": ["book"],
        },
    },
    {
        "name": "compare_books",
        "description": "Compare up to five books from the club corpus side-by-side, including topics, dates, pickers, synopsis, review aggregates, and shared subjects.",
        "input_schema": {
            "type": "object",
            "properties": {
                "books": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 5,
                },
            },
            "required": ["books"],
        },
    },
    {
        "name": "review_summary",
        "description": "Aggregate club reviews for one book: count, average rating, recommendation count, DNF count, discussion average, and short review excerpts.",
        "input_schema": {
            "type": "object",
            "properties": {"book": {"type": "string", "description": "book slug or title"}},
            "required": ["book"],
        },
    },
    {
        "name": "member_history",
        "description": "A member's picks and reviews. Use for 'what has Tom picked', 'what did Jamie think of things'.",
        "input_schema": {
            "type": "object",
            "properties": {"member": {"type": "string", "description": "member name or slug"}},
            "required": ["member"],
        },
    },
    {
        "name": "upcoming_meetings",
        "description": "The club's upcoming/scheduled books (what we're reading next).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_author",
        "description": "Author bio + the books the club has read by them. Use whenever someone asks about an author.",
        "input_schema": {
            "type": "object",
            "properties": {"author": {"type": "string", "description": "author name or slug"}},
            "required": ["author"],
        },
    },
    {
        "name": "club_awards",
        "description": "All awards the club has bestowed (Book of the Year, etc.) with book and year.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "club_stats",
        "description": "Aggregate stats: totals, topic mix, fiction split, books-by-year, picker leaderboard, page stats.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "pending_reviews",
        "description": "Books a member has read but not yet reviewed — answers 'what do I still owe a review for?'. Point them at the /review command to log one.",
        "input_schema": {
            "type": "object",
            "properties": {"member": {"type": "string", "description": "member name or slug"}},
            "required": ["member"],
        },
    },
    {
        "name": "current_club_state",
        "description": "Compact snapshot of Oliver's current operating context: current members, identity links, next meeting attendance status, high-level corpus stats, and recent feedback.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "current_meeting_status",
        "description": "Check roll-call status for the next meeting using club rules: last Tuesday, quorum of 3 of 5 current members, and picker must attend. Read-only.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "meeting_readiness",
        "description": "Combined readiness for the next meeting: attendance, reading status, quorum, "
                       "and who still needs roll-call or reading nudges.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "meeting_campaign",
        "description": "Operational dashboard for the next meeting: current book/date, days left, "
                       "attendance, picker, reading progress, last member contact, and recommended next actions.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "identity_status",
        "description": "Show whether the current Discord speaker is linked to a club member, and which current members still lack Discord identity links.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "recent_feedback",
        "description": "Oliver's recent thumbs-up/down feedback from Discord, joined to the questions that triggered it. Use when reflecting on what has gone well or poorly.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "recent_channel_context",
        "description": "Recent Oliver-visible turns in this Discord channel. This is not the whole channel, just prior messages Oliver handled.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},
        },
    },
    {
        "name": "record_availability",
        "description": "Record the current linked speaker's own explicit availability for the next meeting. Use only when they clearly say they will attend, cannot attend, or are unsure; never infer for others.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["yes", "no", "unsure"]},
            },
            "required": ["status"],
        },
    },
    {
        "name": "propose_action",
        "description": "Stage a non-destructive proposal for admins to review later. Use for suggested corpus patches, reading-order concerns, review nudges, memory repairs, meeting notices, or other club operations that should not be executed directly.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": [
                        "corpus_patch", "reading_order", "review_nudge",
                        "memory_update", "meeting_notice", "other",
                    ],
                },
                "title": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["kind", "title", "body"],
        },
    },
    {
        "name": "open_proposals",
        "description": "List pending admin-review proposals Oliver has staged.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 10}},
        },
    },
    {
        "name": "remember",
        "description": "Save a durable note Oliver should remember across conversations (a member's taste, a club fact, a preference). Private to Oliver.",
        "input_schema": {
            "type": "object",
            "properties": {
                "note": {"type": "string"},
                "subject": {"type": "string", "description": "who/what it's about, e.g. a member slug like 'nick'"},
                "scope": {"type": "string", "enum": ["member", "club", "general"]},
            },
            "required": ["note"],
        },
    },
    {
        "name": "recall",
        "description": "Look up notes Oliver previously saved, by subject and/or text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "e.g. a member slug"},
                "query": {"type": "string", "description": "text to match within notes"},
            },
        },
    },
    {
        "name": "set_reminder",
        "description": "Store a reminder to surface later (e.g. a meeting nudge). Provide an ISO 8601 "
                       "datetime. Reminders are checked about once an hour, so they're nudge-grade — "
                       "good to the hour, not to the minute.",
        "input_schema": {
            "type": "object",
            "properties": {
                "due": {"type": "string", "description": "ISO 8601 datetime, e.g. 2026-06-25T17:00:00Z"},
                "text": {"type": "string"},
            },
            "required": ["due", "text"],
        },
    },
    {
        "name": "send_email",
        "description": "Send a plain-text email from Oliver's rwbookclub.com address. "
                       "Use only when a member explicitly asks Oliver to email someone, "
                       "from Discord. Inbound email replies are sent automatically by the "
                       "runtime; do not use this tool for those. Keep messages brief and club-relevant.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 10,
                    "description": "Recipient email addresses, optionally with display names.",
                },
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "cc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 10,
                },
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "email_status",
        "description": "Check whether Oliver's JMAP email integration is configured.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "record_reading_status",
        "description": "Record the current linked speaker's reading progress for the next/current book. "
                       "Use when they explicitly report where they are in the book, whether by Discord "
                       "or email. Never record progress for someone else.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["not_started", "started", "on_track", "behind", "finished", "paused"],
                },
                "progress": {
                    "type": "string",
                    "description": "Short natural-language status, e.g. 'chapter 4' or 'about halfway'.",
                },
                "page": {"type": "integer", "minimum": 0},
                "percent": {"type": "integer", "minimum": 0, "maximum": 100},
            },
            "required": ["status"],
        },
    },
    {
        "name": "reading_status",
        "description": "Show the reading-progress tracker for the next/current book.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "request_reading_update",
        "description": "Send an email check-in asking one club member for their reading status on the "
                       "next/current book. Use only when an admin or the member explicitly asks for it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "member": {"type": "string", "description": "Member slug or name"},
                "note": {"type": "string", "description": "Optional extra sentence to include."},
            },
            "required": ["member"],
        },
    },
    {
        "name": "request_roll_call_update",
        "description": "Send roll-call email asking for attendance for the next meeting. "
                       "Use only when an admin explicitly asks Oliver to email roll call, "
                       "or when a member asks for their own roll-call email.",
        "input_schema": {
            "type": "object",
            "properties": {
                "member": {
                    "type": "string",
                    "description": "Optional member slug or name. Omit to email all current members with linked email addresses.",
                },
                "note": {"type": "string", "description": "Optional extra sentence to include."},
            },
        },
    },
    {
        "name": "search_discussion",
        "description": "Keyword-search what MEMBERS have actually said in the club's Discord "
                       "channels (#ask-oliver, #general, #book-talk), newest first. This is live "
                       "chat history, NOT the book corpus — reach for it when a question references "
                       "a past conversation or another channel ('didn't we talk about…', 'what did "
                       "someone say in book-talk about…', 'did anyone mention…'). For questions "
                       "about books the club has read, use find_books/get_book instead. Returns "
                       "matching turns tagged with the channel they came from.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "words to look for; a turn must contain every word (AND match)"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_mail_archive",
        "description": "Keyword-search the R/W Book Club mailing-list email archive and future "
                       "archived inbound email. Use when a question asks what the club discussed, "
                       "planned, nominated, voted on, or decided over email. This searches cleaned "
                       "message bodies, not attachments or quoted history.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "words to search for; all terms must match"},
                "member": {"type": "string", "description": "Optional member slug, e.g. jamie, tom, erik"},
                "year_from": {"type": "integer", "minimum": 2016},
                "year_to": {"type": "integer", "minimum": 2016},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_mail_thread",
        "description": "Fetch a chronological cleaned transcript for one email archive thread "
                       "returned by search_mail_archive.",
        "input_schema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["thread_id"],
        },
    },
]


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def dispatch(name: str, tool_input: dict, ctx: dict) -> str:
    """Run a tool. ctx carries {channel_id, speaker, member_slug}. Returns a string."""
    try:
        if name == "find_books":
            return _dump(cr.find_books(tool_input["query"]))
        if name == "search_books":
            return _dump(cr.search_books(**tool_input))
        if name == "get_book":
            return _dump(cr.get_book(tool_input["book"]) or {"error": "no such book"})
        if name == "related_books":
            limit = max(1, min(int(tool_input.get("limit", 8)), 12))
            return _dump(cr.related_books(tool_input["book"], limit=limit) or {"error": "no such book"})
        if name == "compare_books":
            return _dump(cr.compare_books(tool_input["books"]))
        if name == "review_summary":
            return _dump(cr.review_summary(tool_input["book"]) or {"error": "no such book"})
        if name == "member_history":
            return _dump(cr.member_history(tool_input["member"]) or {"error": "no such member"})
        if name == "upcoming_meetings":
            return _dump(cr.upcoming_meetings())
        if name == "get_author":
            return _dump(cr.get_author(tool_input["author"]) or {"error": "no such author"})
        if name == "club_awards":
            return _dump(cr.awards())
        if name == "club_stats":
            return _dump(cr.club_stats())
        if name == "pending_reviews":
            return _dump(cr.pending_reviews(tool_input["member"]) or {"error": "no such member"})
        if name == "current_club_state":
            return _dump(meeting_rules.summarize_club_state())
        if name == "current_meeting_status":
            return _dump(meeting_rules.meeting_status())
        if name == "meeting_readiness":
            return _dump(_meeting_readiness_snapshot())
        if name == "meeting_campaign":
            return _dump(meeting_campaign.snapshot())
        if name == "identity_status":
            member_slug = ctx.get("member_slug")
            identities = db.list_member_identities()
            linked = {r["member_slug"] for r in identities}
            email_links = db.list_member_emails()
            email_linked = {r["member_slug"] for r in email_links}
            current = [m for m in cr.members() if m.get("isCurrent")]
            return _dump({
                "speakerUserId": ctx.get("speaker_user_id"),
                "speakerMemberSlug": member_slug,
                "speakerMember": cr.find_member(member_slug) if member_slug else None,
                "linkedCurrentMembers": sorted(linked),
                "emailLinkedCurrentMembers": sorted(email_linked),
                "missingCurrentMembers": [
                    {"slug": m["slug"], "name": m.get("name")}
                    for m in current if m["slug"] not in linked
                ],
                "missingEmailCurrentMembers": [
                    {"slug": m["slug"], "name": m.get("name")}
                    for m in current if m["slug"] not in email_linked
                ],
            })
        if name == "recent_feedback":
            return _dump(db.feedback_stats())
        if name == "recent_channel_context":
            limit = max(1, min(int(tool_input.get("limit", 12)), 20))
            return _dump(db.recent_messages(str(ctx.get("channel_id") or ""), limit=limit))
        if name == "search_discussion":
            limit = max(1, min(int(tool_input.get("limit", 12)), 20))
            rows = db.search_conversations(tool_input["query"], limit=limit)
            for r in rows:
                try:
                    channel_key = int(r["channel_id"])
                except (TypeError, ValueError):
                    channel_key = r["channel_id"]
                r["channel"] = config.CHANNEL_NAMES.get(channel_key, r["channel_id"])
                r["content"] = (r["content"] or "")[:300]  # keep tool result compact
            return _dump(rows)
        if name == "search_mail_archive":
            limit = max(1, min(int(tool_input.get("limit", 8)), 20))
            rows = db.search_mail_archive(
                tool_input["query"],
                member_slug=tool_input.get("member"),
                year_from=tool_input.get("year_from"),
                year_to=tool_input.get("year_to"),
                limit=limit,
            )
            for r in rows:
                r["snippet"] = (r.get("snippet") or "")[:500]
            return _dump(rows)
        if name == "get_mail_thread":
            limit = max(1, min(int(tool_input.get("limit", 50)), 100))
            thread = db.get_mail_thread(tool_input["thread_id"], limit=limit)
            if not thread:
                return _dump({"error": "no such mail thread"})
            for msg in thread["messages"]:
                msg["body_clean"] = (msg.get("body_clean") or "")[:1000]
            return _dump(thread)
        if name == "record_availability":
            member_slug = ctx.get("member_slug")
            if not member_slug:
                return _dump({"error": "speaker is not linked to a club member"})
            status = tool_input["status"]
            meeting = meeting_rules.next_meeting()
            db.upsert_roll_call(
                meeting_key=meeting["meetingKey"],
                channel_id=ctx.get("channel_id"),
                opened_by="oliver",
            )
            db.set_attendance(
                meeting_key=meeting["meetingKey"],
                member_slug=member_slug,
                status=status,
                updated_by_user_id=ctx.get("speaker_user_id"),
                source=("email" if str(ctx.get("speaker_user_id") or "").startswith("email:") else "chat"),
            )
            db.add_activity(
                "roll_call_update",
                "Roll-call response recorded",
                f"Member: {member_slug}\nStatus: {status}\nMeeting: {meeting['meetingKey']}",
            )
            return _dump({"saved": True, "meetingStatus": meeting_rules.meeting_status(meeting["meetingKey"])})
        if name == "propose_action":
            pid = db.add_proposal(
                kind=tool_input["kind"],
                title=tool_input["title"],
                body=tool_input["body"],
                channel_id=ctx.get("channel_id"),
                source_user_id=ctx.get("speaker_user_id"),
            )
            return _dump({"saved": True, "id": pid})
        if name == "open_proposals":
            limit = max(1, min(int(tool_input.get("limit", 10)), 10))
            return _dump(db.list_proposals(limit=limit))
        if name == "remember":
            scope = tool_input.get("scope", "general")
            # Club-scope notes are injected into *every* future turn's context
            # (oliver._question_block), so only a linked club member can shape
            # that shared lore. From anyone else, record it as a general note
            # rather than letting it poison the global overview.
            if scope == "club" and not ctx.get("member_slug"):
                scope = "general"
            mid = db.add_memory(
                tool_input["note"],
                scope=scope,
                subject=tool_input.get("subject"),
                source=ctx.get("speaker"),
                source_user_id=ctx.get("speaker_user_id"),
                source_message_id=ctx.get("source_message_id"),
            )
            return _dump({"saved": True, "id": mid, "scope": scope})
        if name == "recall":
            return _dump(db.get_memories(subject=tool_input.get("subject"), query=tool_input.get("query")))
        if name == "set_reminder":
            rid = db.add_reminder(
                tool_input["due"], tool_input["text"],
                channel_id=ctx.get("channel_id"), created_by=ctx.get("speaker"),
            )
            return _dump({"saved": True, "id": rid})
        if name == "send_email":
            if str(ctx.get("channel_id") or "").startswith("email:"):
                return _dump({
                    "error": "inbound email replies are sent automatically; write response text instead"
                })
            if not email_jmap.enabled():
                return _dump({"error": "email is not configured"})
            recipient_error = email_policy.validate_model_email_recipients(
                to=tool_input["to"],
                cc=tool_input.get("cc"),
            )
            if recipient_error:
                return _dump({"error": recipient_error})
            result = outbound.send(
                to=tool_input["to"],
                subject=tool_input["subject"],
                body=tool_input["body"],
                cc=tool_input.get("cc"),
            )
            db.add_activity(
                "email_sent",
                "Email sent",
                f"To: {', '.join(result.get('to') or [])}\nSubject: {result.get('subject')}\nEmail ID: {result.get('emailId')}",
            )
            return _dump({"sent": True, **result})
        if name == "email_status":
            return _dump({
                "configured": email_jmap.enabled(),
                "address": config.OLIVER_EMAIL_ADDRESS,
                "inbox": f"{config.OLIVER_EMAIL_INBOX_PARENT}/{config.OLIVER_EMAIL_INBOX_FOLDER}",
                "sent": f"{config.OLIVER_EMAIL_SENT_PARENT}/{config.OLIVER_EMAIL_SENT_FOLDER}",
            })
        if name == "record_reading_status":
            member_slug = ctx.get("member_slug")
            if not member_slug:
                return _dump({"error": "speaker is not linked to a club member"})
            meeting = meeting_rules.next_meeting()
            db.set_reading_status(
                meeting_key=meeting["meetingKey"],
                member_slug=member_slug,
                status=tool_input["status"],
                progress=tool_input.get("progress"),
                page=tool_input.get("page"),
                percent=tool_input.get("percent"),
                source=("email" if str(ctx.get("speaker_user_id") or "").startswith("email:") else "discord"),
                updated_by=ctx.get("speaker_user_id"),
            )
            db.add_activity(
                "reading_update",
                "Reading status recorded",
                f"Member: {member_slug}\nStatus: {tool_input['status']}\nProgress: {tool_input.get('progress') or '-'}\nMeeting: {meeting['meetingKey']}",
            )
            return _dump({"saved": True, "readingStatus": _reading_status_snapshot(meeting)})
        if name == "reading_status":
            return _dump(_reading_status_snapshot(meeting_rules.next_meeting()))
        if name == "request_reading_update":
            if str(ctx.get("channel_id") or "").startswith("email:"):
                return _dump({"error": "email check-ins cannot be initiated from inbound email"})
            if not email_jmap.enabled():
                return _dump({"error": "email is not configured"})
            member = cr.find_member(tool_input["member"])
            if not member:
                return _dump({"error": "no such member"})
            speaker_user_id = str(ctx.get("speaker_user_id") or "")
            if ctx.get("member_slug") != member["slug"] and speaker_user_id != str(config.ADMIN_USER_ID):
                return _dump({"error": "only an admin can request check-ins for other members"})
            email = db.email_for_member(member["slug"])
            if not email:
                return _dump({"error": f"{member['name']} has no linked email address"})
            meeting = meeting_rules.next_meeting()
            book = meeting.get("book") or {}
            title = book.get("title") or "the current book"
            existing = db.reading_status_for_member(meeting["meetingKey"], member["slug"])
            if existing and existing["status"] == "finished":
                db.add_activity(
                    "reading_checkin_skipped",
                    "Reading check-in skipped",
                    f"Member: {member['slug']}\nReason: already finished\nBook: {title}",
                )
                return _dump({
                    "sent": False,
                    "member": member["slug"],
                    "reason": f"{member['name']} is already marked finished for {title}",
                    "readingStatus": _reading_status_snapshot(meeting),
                })
            timing = _days_until_text(meeting["date"])
            meeting_when = f"{meeting['date']}" + (f" ({timing})" if timing else "")
            extra = f"\n\n{tool_input['note'].strip()}" if tool_input.get("note") else ""
            body = (
                f"Hi {member['name']},\n\n"
                f"Quick reading check-in for {title}. The meeting is {meeting_when}. "
                "Where are you in the book, and do you feel on track?\n\n"
                "Reply with something short like \"halfway and on track\", "
                "\"page 120, behind\", or \"finished\" and I'll update the tracker."
                f"{extra}"
            )
            subject = f"Reading check-in: {title}"
            sent = outbound.send(
                to=[email["email"]], subject=subject, body=body,
                track={"meeting_key": meeting["meetingKey"], "member_slug": member["slug"], "kind": "reading_checkin"},
            )
            db.add_activity(
                "email_sent",
                "Reading check-in email sent",
                f"Member: {member['slug']}\nTo: {email['email']}\nSubject: {subject}\nEmail ID: {sent.get('emailId')}",
            )
            return _dump({"sent": True, "member": member["slug"], **sent})
        if name == "request_roll_call_update":
            if str(ctx.get("channel_id") or "").startswith("email:"):
                return _dump({"error": "roll-call emails cannot be initiated from inbound email"})
            if not email_jmap.enabled():
                return _dump({"error": "email is not configured"})
            speaker_user_id = str(ctx.get("speaker_user_id") or "")
            requested_member = tool_input.get("member")
            if requested_member:
                member = cr.find_member(requested_member)
                if not member:
                    return _dump({"error": "no such member"})
                if ctx.get("member_slug") != member["slug"] and speaker_user_id != str(config.ADMIN_USER_ID):
                    return _dump({"error": "only an admin can request roll-call emails for other members"})
                targets = [member]
            else:
                if speaker_user_id != str(config.ADMIN_USER_ID):
                    return _dump({"error": "only an admin can email roll call to all members"})
                targets = sorted(
                    [m for m in cr.members() if m.get("isCurrent")],
                    key=lambda m: m.get("name") or m["slug"],
                )
            status = meeting_rules.meeting_status()
            meeting = status["meeting"]
            attendance = {r["memberSlug"]: r["status"] for r in status["attendance"]}
            skipped = []
            filtered_targets = []
            for member in targets:
                member_status = attendance.get(member["slug"], "pending")
                if member_status != "pending":
                    skipped.append({"member": member["slug"], "reason": f"already {member_status}"})
                    continue
                filtered_targets.append(member)
            sent_rows = []
            missing = []
            note = tool_input.get("note")
            for member in filtered_targets:
                email = db.email_for_member(member["slug"])
                if not email:
                    missing.append({"member": member["slug"], "reason": "no linked email address"})
                    continue
                subject = _roll_call_subject(status)
                body = _roll_call_email_body(member.get("name") or member["slug"], status, note=note)
                sent = outbound.send(
                    to=[email["email"]], subject=subject, body=body,
                    track={"meeting_key": meeting["meetingKey"], "member_slug": member["slug"], "kind": "roll_call"},
                )
                db.add_activity(
                    "email_sent",
                    "Roll-call email sent",
                    f"Member: {member['slug']}\nTo: {email['email']}\nSubject: {subject}\nEmail ID: {sent.get('emailId')}",
                )
                sent_rows.append({"member": member["slug"], **sent})
            if sent_rows:
                db.upsert_roll_call(
                    meeting_key=meeting["meetingKey"],
                    channel_id=ctx.get("channel_id"),
                    opened_by="email-tool",
                )
            return _dump({
                "sent": sent_rows,
                "skipped": skipped,
                "missing": missing,
                "meetingStatus": meeting_rules.meeting_status(meeting["meetingKey"]),
            })
        return _dump({"error": f"unknown tool {name}"})
    except Exception as e:  # noqa: BLE001 - surface tool errors to the model, don't crash the loop
        # Also log so the operator sees it — bare error strings to the model
        # used to be invisible to anyone watching the bot.
        log.exception("tool %s failed (input=%r)", name, tool_input)
        return _dump({"error": f"{type(e).__name__}: {e}"})


def _reading_status_snapshot(meeting: dict) -> dict:
    rows = {r["member_slug"]: r for r in db.reading_status_for_meeting(meeting["meetingKey"])}
    members = [m for m in cr.members() if m.get("isCurrent")]
    statuses = []
    for member in sorted(members, key=lambda m: m.get("name") or m["slug"]):
        row = rows.get(member["slug"])
        statuses.append({
            "member": member.get("name"),
            "memberSlug": member["slug"],
            "status": row.get("status") if row else "unknown",
            "progress": row.get("progress") if row else None,
            "page": row.get("page") if row else None,
            "percent": row.get("percent") if row else None,
            "source": row.get("source") if row else None,
            "updatedAt": row.get("updated_at") if row else None,
        })
    return {
        "meeting": meeting,
        "book": meeting.get("book"),
        "statuses": statuses,
    }


def _meeting_readiness_snapshot() -> dict:
    campaign = meeting_campaign.snapshot()
    return {
        **campaign,
        "reading": _reading_status_snapshot(campaign["meeting"]),
        "counts": {
            **campaign["counts"],
            "attendingAndFinished": len([
                m for m in campaign["members"]
                if m["attendance"] == "yes" and m["reading"] == "finished"
            ]),
            "attendingNotFinished": len([
                m for m in campaign["members"]
                if m["attendance"] == "yes" and m["reading"] != "finished"
            ]),
        },
    }


def _days_until_text(meeting_date: str) -> str:
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


def _roll_call_subject(status: dict) -> str:
    meeting = status["meeting"]
    title = (meeting.get("book") or {}).get("title") or "the next meeting"
    return f"Roll call: {title} on {meeting['date']}"


def _roll_call_email_body(member_name: str, status: dict, *, note: str | None = None) -> str:
    meeting = status["meeting"]
    title = (meeting.get("book") or {}).get("title") or "the next meeting"
    timing = _days_until_text(meeting["date"])
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
