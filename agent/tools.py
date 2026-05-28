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

from agent import corpus_read as cr
from agent import db

log = logging.getLogger("oliver.tools")

# Tool definitions sent to the API. Order is stable so the prompt-cache prefix
# (tools render before system) stays valid across requests.
TOOLS = [
    # Anthropic server-side web search — handled by the platform, not by dispatch().
    # Use sparingly (see SYSTEM_PROMPT): corpus tools first; web only for specific
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
        "description": "Store a reminder to surface later (e.g. a meeting nudge). Provide an ISO 8601 datetime.",
        "input_schema": {
            "type": "object",
            "properties": {
                "due": {"type": "string", "description": "ISO 8601 datetime, e.g. 2026-06-25T17:00:00Z"},
                "text": {"type": "string"},
            },
            "required": ["due", "text"],
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
        if name == "remember":
            mid = db.add_memory(
                tool_input["note"],
                scope=tool_input.get("scope", "general"),
                subject=tool_input.get("subject"),
                source=ctx.get("speaker"),
            )
            return _dump({"saved": True, "id": mid})
        if name == "recall":
            return _dump(db.get_memories(subject=tool_input.get("subject"), query=tool_input.get("query")))
        if name == "set_reminder":
            rid = db.add_reminder(
                tool_input["due"], tool_input["text"],
                channel_id=ctx.get("channel_id"), created_by=ctx.get("speaker"),
            )
            return _dump({"saved": True, "id": rid})
        return _dump({"error": f"unknown tool {name}"})
    except Exception as e:  # noqa: BLE001 - surface tool errors to the model, don't crash the loop
        # Also log so the operator sees it — bare error strings to the model
        # used to be invisible to anyone watching the bot.
        log.exception("tool %s failed (input=%r)", name, tool_input)
        return _dump({"error": f"{type(e).__name__}: {e}"})
