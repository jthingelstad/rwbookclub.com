"""Oliver's tool surface: Anthropic tool schemas + a dispatcher.

Phase 2 tools are all read-only (corpus) or write to SQLite (memory/reminders) —
nothing irreversible. `dispatch` is called by the agent loop with the tool name,
the model's input, and a per-turn context (channel + speaker). Results are returned
as compact JSON strings for the model to read.
"""

from __future__ import annotations

import json

from agent import corpus_read as cr
from agent import db

# Tool definitions sent to the API. Order is stable so the prompt-cache prefix
# (tools render before system) stays valid across requests.
TOOLS = [
    {
        "name": "search_books",
        "description": "Find books the club has read by free-text and/or filters. Use for "
                       "'have we read anything about X', 'books by Y', 'our sci-fi reads'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "free-text match on title/subtitle/synopsis/author/topic"},
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
        return _dump({"error": f"{type(e).__name__}: {e}"})
