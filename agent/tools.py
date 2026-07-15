"""Oliver's tool surface: Anthropic tool schemas + a dispatcher.

Two kinds of tools live here:
- **Server-side** (Anthropic-hosted): `web_search` — the platform resolves it,
  `dispatch()` never sees it because the response blocks have type
  `server_tool_use`, not `tool_use`.
- **Client-side**: stable schemas and the single authorization/registry gate live here;
  capability implementations live in `tool_handlers/` and actor-scoped private readers in
  `model_readers.py`. `dispatch` is called by the agent loop with the tool
  name, model's input, and per-turn context; returns a compact JSON string.

Tool errors are caught and returned to the model as `{"error": ...}` so the
loop survives — but also `log.exception` so the operator can see what broke.
"""

from __future__ import annotations

import json
import logging

from agent import access
from agent import corpus_read as cr
from agent import db
from agent.tool_handlers import mail, meeting, memory, picking
from agent.tool_handlers.context import RequestContext

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
        "description": "A member's picks, the meetings they hosted, and their reviews. Use for 'what has Tom picked', 'how many meetings has Erik hosted', 'what did Jamie think of things'.",
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
        "name": "horizon",
        "description": "Read-only five-book runway: scheduled upcoming books followed by open "
                       "picker slots under the least-recently-scheduled fairness rule. Use for "
                       "what comes after the current book, how thin the runway is, or whose open "
                       "slot appears next. Empty slots are status, never a nudge or assignment.",
        "input_schema": {
            "type": "object",
            "properties": {
                "depth": {"type": "integer", "minimum": 1, "maximum": 8, "default": 5},
            },
        },
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
        "name": "club_lists",
        "description": "The club's curated book lists (e.g. 'Books of the Year', 'Our Favorite "
                       "Books') — each with a description and its books. For a member's own lists, "
                       "use member_history instead.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "club_stats",
        "description": "Aggregate stats: totals, topic mix, fiction split, books-by-year, picker leaderboard, host leaderboard (meetings hosted per member), page stats.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "pending_reviews",
        "description": "Books a member has read but not yet reviewed — answers 'what do I still owe a review for?'. Reviewing is done in the web app: point them to /oliver my-club (Reviews tab) to log one.",
        "input_schema": {
            "type": "object",
            "properties": {"member": {"type": "string", "description": "member name or slug"}},
            "required": ["member"],
        },
    },
    {
        "name": "current_club_state",
        "description": "Compact snapshot of current members, aggregate next-meeting status, and "
                       "high-level corpus stats. Non-admin members receive only their own "
                       "attendance row and no identity-link or feedback details.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "current_meeting_status",
        "description": "The source of truth for the NEXT meeting: its canonical date, the book, "
                       "and the picker, plus aggregate roll-call status under club rules. "
                       "A member sees only their own attendance row; admin sees the full roster. "
                       "Call this to verify any meeting date/time/book a member states. Read-only.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "meeting_readiness",
        "description": "Combined readiness for the next meeting. Members see aggregate counts "
                       "plus only their own attendance/reading row; admin sees the full roster "
                       "and who still needs a nudge.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "meeting_campaign",
        "description": "Admin-only operational dashboard for the next meeting: current book/date, "
                       "attendance, reading progress, last member contact, and next actions.",
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
        "description": "Look up club lore and notes about the linked speaker. Member-private notes "
                       "for anyone else are unavailable; admins may audit another member by subject.",
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
        "description": "Show reading progress for the next/current book. A member sees only their "
                       "own row; admin sees the full tracker.",
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
        "description": "Keyword-search shared Discord/mailing-list discussion plus the linked "
                       "speaker's own 1:1 email threads — newest first. Another member's private "
                       "email is unavailable unless the speaker is the admin. This is conversation memory, NOT "
                       "the book corpus. Reach for it whenever someone refers to an earlier exchange "
                       "on any medium ('the books we went over in email', 'didn't we talk about…', "
                       "'what did someone say in book-talk'). Each result is tagged with its medium "
                       "(email / mailing list / Discord), who said it, and whether it was a member's "
                       "turn or YOUR reply — so you can tell email from Discord and see what you "
                       "yourself sent. A member may pass only their own slug; admins may scope to "
                       "another member for repair/audit. For book facts, use find_books/get_book.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "words to look for; a turn must contain every word (AND match)"},
                "member": {"type": "string", "description": "Optional member slug (e.g. jamie) — scope to your conversations with that person across mediums"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_mail_archive",
        "description": "Keyword-search the R/W Book Club email archive — the shared mailing list plus "
                       "the linked speaker's own 1:1 inbound mail and Oliver replies. Other members' "
                       "private threads are unavailable unless the speaker is the admin. Use "
                       "when a question asks what the club (or you) discussed, planned, nominated, "
                       "voted on, or decided over email. Searches cleaned message bodies, not "
                       "attachments or quoted history.",
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
                       "returned by search_mail_archive — shared list mail or the linked speaker's "
                       "own private thread, with addresses omitted. Admins may audit any thread.",
        "input_schema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["thread_id"],
        },
    },
    {
        "name": "club_timeline",
        "description": "Read the club's historical TIMELINE — the structured log of what has "
                       "happened (and is scheduled) in the club's life: meetings, book "
                       "nominations/votes/picks, dinners and hosting, members joining/leaving, "
                       "shared milestones, and club/tooling moments. Use it for 'when did…', "
                       "'what's the history of…', 'what has <member> been part of…' questions. "
                       "This is the curated event spine, distinct from raw chat (search_discussion) "
                       "and raw email (search_mail_archive). Newest first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "member": {"type": "string", "description": "Optional member slug or name to scope to."},
                "category": {"type": "string",
                             "enum": ["meeting", "selection", "social", "member_life", "club", "reading", "meeting_ops"],
                             "description": "Optional category filter."},
                "since": {"type": "string", "description": "Optional earliest date (YYYY-MM-DD)."},
                "until": {"type": "string", "description": "Optional latest date (YYYY-MM-DD)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
    },
    {
        "name": "record_timeline_event",
        "description": "Record ONE durable club event onto the timeline, when a member tells you "
                       "something worth preserving in the club's history — a meeting being set, a "
                       "book picked, a dinner planned, someone hosting, a member joining/leaving, or "
                       "a clearly-shared milestone (new job, a move, travel that affects attendance). "
                       "This is the shared chronicle, NOT your private notes (use `remember` for "
                       "those). Only record operational facts and shared milestones — never anything "
                       "sensitive (health, finances, relationships, conflict). Record only what the "
                       "member actually stated; don't infer or embellish.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string",
                             "enum": ["meeting", "selection", "social", "member_life", "club", "reading"]},
                "kind": {"type": "string",
                         "description": "Event kind within the category, e.g. book_picked, dinner, "
                                        "hosting, member_away, member_milestone, meeting_held."},
                "date": {"type": "string", "description": "When it happened/will happen (YYYY-MM-DD)."},
                "summary": {"type": "string", "description": "One factual sentence describing the event."},
                "member": {"type": "string",
                           "description": "Optional member slug/name the event is about; omit for club-wide."},
            },
            "required": ["category", "kind", "date", "summary"],
        },
    },
    {
        "name": "book_cloud_add",
        "description": "Quietly record a book a member genuinely referenced — named, compared, "
                       "recommended, objected to — into the club's private Book Cloud. The REASON "
                       "it came up is the whole point ('mentioned in chat' is a failed reason); a "
                       "reference is not a nomination unless the member says so. Silent "
                       "bookkeeping: never reply, ask a follow-up, or interrogate intent just "
                       "because a book was named.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "reason": {"type": "string",
                           "description": "Why it came up — the specific connection, comparison, or context."},
                "author": {"type": "string"},
                "reason_kind": {"type": "string",
                                "enum": ["nomination", "recommendation", "comparison", "caution",
                                         "context", "inquiry", "joke"]},
            },
            "required": ["title", "reason"],
        },
    },
    {
        "name": "book_cloud_recent",
        "description": "Read the club's private book-mention memory — books members have "
                       "referenced but "
                       "(usually) not read. Default: raw recent mentions, newest first. Pass "
                       "titles=true for the aggregated orbit view (one row per title: first/last "
                       "mention, who, how often, recent reasons). Frame results as books orbiting "
                       "the conversation — not a queue, ranking, or commitment. In member-facing "
                       "language say books we've been circling or informal mentions; do not say "
                       "Book Cloud unless the member used that term first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Optional filter over title/author/reason."},
                "titles": {"type": "boolean", "description": "Aggregated one-row-per-title view."},
                "member": {"type": "string", "description": "With titles=true: only titles this member slug has mentioned."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        },
    },
    {
        "name": "pick_fit",
        "description": "EVALUATE a book-pick CANDIDATE against this club: whether we've read it, "
                       "its own Book Cloud history (who floated it, when, why), its nearest "
                       "neighbors on our shelf with the club's actual verdicts (ratings AND "
                       "discussion quality), every member's taste lens, and our coverage. Use it "
                       "for each serious candidate when someone is weighing a pick. It does NOT "
                       "include current reception/adaptation news — web_search that separately. "
                       "Never invent member reactions beyond the lens data returned.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "author": {"type": "string", "description": "Helps disambiguate the lookup."},
                "isbn": {"type": "string"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "pick_prospects",
        "description": "DISCOVER pick candidates for a member. Works best WITH a direction — this "
                       "club picks topic-first, so if the member hasn't said where they want to "
                       "go, ask them before calling this. Returns: their taste profile, "
                       "direction-led web_search angles (fresh, never-mentioned books are usually "
                       "the best answer), the club's coverage gaps, and — as supporting color, "
                       "not the main course — the Book Cloud's unread orbit and loved authors "
                       "with unread works. Then web_search the angles and pick_fit the best 2-3.",
        "input_schema": {
            "type": "object",
            "properties": {
                "member": {"type": "string",
                           "description": "Member slug; defaults to whoever is asking."},
                "direction": {"type": "string",
                              "description": "Optional steer, e.g. 'fiction' or 'something in the history lane'."},
            },
        },
    },
]


CORE_NAMES = frozenset({
    "find_books", "search_books", "get_book", "related_books", "compare_books",
    "review_summary", "member_history", "upcoming_meetings", "get_author", "club_lists",
    "club_stats", "identity_status", "recent_feedback", "propose_action", "open_proposals",
})


def _member_slug(value: str | None) -> str | None:
    member = cr.find_member(value) if value else None
    return member.get("slug") if member else None


def _handle_core(name: str, tool_input: dict, request: RequestContext):
    if name == "find_books":
        return cr.find_books(tool_input["query"])
    if name == "search_books":
        return cr.search_books(**tool_input)
    if name == "get_book":
        return cr.get_book(tool_input["book"]) or {"error": "no such book"}
    if name == "related_books":
        limit = max(1, min(int(tool_input.get("limit", 8)), 12))
        return cr.related_books(tool_input["book"], limit=limit) or {"error": "no such book"}
    if name == "compare_books":
        return cr.compare_books(tool_input["books"])
    if name == "review_summary":
        return cr.review_summary(tool_input["book"]) or {"error": "no such book"}
    if name == "member_history":
        return cr.member_history(tool_input["member"]) or {"error": "no such member"}
    if name == "upcoming_meetings":
        return cr.upcoming_meetings()
    if name == "get_author":
        return cr.get_author(tool_input["author"]) or {"error": "no such author"}
    if name == "club_lists":
        return [item for item in cr.lists() if item.get("scope") == "club"]
    if name == "club_stats":
        return cr.club_stats()
    if name == "identity_status":
        member_slug = request.member_slug
        linked = {row["member_slug"] for row in db.list_member_identities()}
        email_linked = {row["member_slug"] for row in db.list_member_emails()}
        sms_linked = {row["member_slug"] for row in db.list_member_sms()}
        website_linked = {row["member_slug"] for row in db.list_member_websites()}
        current = cr.human_current_members()
        if not request.actor.is_admin:
            return {
                "speakerUserId": request.speaker_user_id,
                "speakerMemberSlug": member_slug,
                "speakerMember": cr.find_member(member_slug) if member_slug else None,
                "discordLinked": member_slug in linked,
                "emailLinked": member_slug in email_linked,
                "smsLinked": member_slug in sms_linked,
                "websiteLinked": member_slug in website_linked,
            }
        return {
            "speakerUserId": request.speaker_user_id,
            "speakerMemberSlug": member_slug,
            "speakerMember": cr.find_member(member_slug) if member_slug else None,
            "linkedCurrentMembers": sorted(linked),
            "emailLinkedCurrentMembers": sorted(email_linked),
            "smsLinkedCurrentMembers": sorted(sms_linked),
            "websiteLinkedCurrentMembers": sorted(website_linked),
            "missingCurrentMembers": [
                {"slug": member["slug"], "name": member.get("name")}
                for member in current if member["slug"] not in linked
            ],
            "missingEmailCurrentMembers": [
                {"slug": member["slug"], "name": member.get("name")}
                for member in current if member["slug"] not in email_linked
            ],
        }
    if name == "recent_feedback":
        return db.feedback_stats()
    if name == "propose_action":
        proposal_id = db.add_proposal(
            kind=tool_input["kind"],
            title=tool_input["title"],
            body=tool_input["body"],
            channel_id=request.channel_id,
            source_user_id=request.speaker_user_id,
        )
        return {"saved": True, "id": proposal_id}
    if name == "open_proposals":
        limit = max(1, min(int(tool_input.get("limit", 10)), 10))
        return db.list_proposals(limit=limit)
    raise KeyError(name)


def _build_registry():
    registry = {name: _handle_core for name in CORE_NAMES}
    for capability in (meeting, memory, mail, picking):
        overlap = set(registry) & capability.NAMES
        if overlap:
            raise RuntimeError(f"duplicate tool handler registration: {sorted(overlap)}")
        registry.update({name: capability.handle for name in capability.NAMES})
    expected = {
        definition["name"] for definition in TOOLS
        if definition.get("type") != "web_search_20250305"
    }
    if set(registry) != expected:
        raise RuntimeError(
            f"tool registry/schema mismatch: missing={sorted(expected - set(registry))}, "
            f"extra={sorted(set(registry) - expected)}"
        )
    return registry


# The one auditable client-tool registry. Authorization remains centralized in dispatch below.
TOOL_HANDLERS = _build_registry()


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def dispatch(name: str, tool_input: dict, ctx: dict) -> str:
    """Authorize and dispatch one tool with identity resolved only from trusted runtime context."""
    try:
        actor = access.actor_from_ctx(ctx)
        if error := access.tool_access_error(name, actor):
            log.warning(
                "tool access denied: tool=%s member=%s admin=%s",
                name, actor.member_slug, actor.is_admin,
            )
            return _dump({"error": error})
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return _dump({"error": f"unknown tool {name}"})
        request = RequestContext.from_runtime(ctx, actor=actor)
        return _dump(handler(name, tool_input, request))
    except Exception as exc:
        log.exception("tool %s failed (input=%r)", name, tool_input)
        return _dump({"error": f"{type(exc).__name__}: {exc}"})


# Compatibility aliases for focused tests and internal callers while implementation lives in the
# capability modules. The dispatcher itself reaches these only through TOOL_HANDLERS.
def _member_lenses(ctx: dict | None = None) -> dict:
    runtime = ctx or {}
    request = RequestContext.from_runtime(runtime, actor=access.actor_from_ctx(runtime))
    return picking.member_lenses(request)


def _pick_fit(tool_input: dict, ctx: dict) -> dict:
    request = RequestContext.from_runtime(ctx, actor=access.actor_from_ctx(ctx))
    return picking.pick_fit(tool_input, request)


def _pick_prospects(tool_input: dict, ctx: dict) -> dict:
    request = RequestContext.from_runtime(ctx, actor=access.actor_from_ctx(ctx))
    return picking.pick_prospects(tool_input, request)


_meeting_status_snapshot = meeting.meeting_status_snapshot
_current_club_state_snapshot = meeting.current_club_state_snapshot
_reading_status_snapshot = meeting.reading_status_snapshot
_meeting_readiness_snapshot = meeting.meeting_readiness_snapshot
