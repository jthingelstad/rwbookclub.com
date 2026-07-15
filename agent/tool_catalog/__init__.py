"""Capability-grouped, stable-order tool contract catalog."""

from __future__ import annotations

from typing import Any

from agent.tool_catalog import core, mail, meeting, memory, picking, server

ToolDefinition = dict[str, Any]

CAPABILITY_TOOLS: dict[str, list[ToolDefinition]] = {
    "server": server.TOOLS,
    "core": core.TOOLS,
    "meeting": meeting.TOOLS,
    "memory": memory.TOOLS,
    "mail": mail.TOOLS,
    "picking": picking.TOOLS,
}

_ORDER = (
    "web_search",
    "find_books",
    "search_books",
    "get_book",
    "related_books",
    "compare_books",
    "review_summary",
    "member_history",
    "upcoming_meetings",
    "horizon",
    "get_author",
    "club_lists",
    "club_stats",
    "pending_reviews",
    "current_club_state",
    "current_meeting_status",
    "meeting_readiness",
    "meeting_campaign",
    "identity_status",
    "recent_feedback",
    "recent_channel_context",
    "record_availability",
    "propose_action",
    "open_proposals",
    "remember",
    "recall",
    "set_reminder",
    "send_email",
    "email_status",
    "record_reading_status",
    "reading_status",
    "request_reading_update",
    "request_roll_call_update",
    "search_discussion",
    "search_mail_archive",
    "get_mail_thread",
    "club_timeline",
    "record_timeline_event",
    "book_cloud_add",
    "book_cloud_recent",
    "pick_fit",
    "pick_prospects",
)


def _build_catalog() -> list[ToolDefinition]:
    by_name: dict[str, ToolDefinition] = {}
    for capability, definitions in CAPABILITY_TOOLS.items():
        for definition in definitions:
            name = definition["name"]
            if name in by_name:
                raise RuntimeError(f"duplicate tool schema {name!r} in {capability}")
            by_name[name] = definition
    expected = set(_ORDER)
    if set(by_name) != expected:
        raise RuntimeError(
            f"tool catalog mismatch: missing={sorted(expected - set(by_name))}, "
            f"extra={sorted(set(by_name) - expected)}"
        )
    return [by_name[name] for name in _ORDER]


# Exact order is part of the prompt-cache contract.
TOOLS: list[ToolDefinition] = _build_catalog()
