"""Member memory, conversation retrieval, and reminder capabilities."""

from __future__ import annotations

from agent import access, config, db, model_readers
from agent import corpus_read as cr
from agent.tool_handlers.context import RequestContext

NAMES = frozenset(
    {
        "recent_channel_context",
        "search_discussion",
        "remember",
        "recall",
        "set_reminder",
    }
)


def _member_slug(value: str | None) -> str | None:
    member = cr.find_member(value) if value else None
    return member.get("slug") if member else None


def handle(name: str, tool_input: dict, request: RequestContext):
    actor = request.actor
    if name == "recent_channel_context":
        limit = max(1, min(int(tool_input.get("limit", 12)), 20))
        return model_readers.recent_channel(
            actor=actor, channel_id=str(request.channel_id or ""), limit=limit
        )
    if name == "search_discussion":
        limit = max(1, min(int(tool_input.get("limit", 12)), 20))
        requested = tool_input.get("member")
        target = _member_slug(requested) if requested else None
        if requested and not target:
            return {"error": f"no such member: {requested}"}
        if target and not access.can_access_member(actor, target):
            return {"error": "another member's private conversation history is unavailable"}
        rows = model_readers.search_discussion(
            actor=actor, query=tool_input["query"], member_slug=target, limit=limit
        )
        out = []
        for row in rows:
            try:
                channel_key = int(row["channel_id"])
            except TypeError, ValueError:
                channel_key = row["channel_id"]
            out.append(
                {
                    "medium": db.conversation_medium(row["channel_id"]),
                    "channel": config.CHANNEL_NAMES.get(channel_key, row["channel_id"]),
                    "who": row.get("speaker"),
                    "member": row.get("member_slug"),
                    "role": row["role"],
                    "when": row.get("created_at"),
                    "content": (row["content"] or "")[:300],
                }
            )
        return out
    if name == "remember":
        scope = tool_input.get("scope") or "member"
        subject = tool_input.get("subject")
        if scope == "member":
            target = _member_slug(subject) if subject else actor.member_slug
            if subject and not target:
                return {"error": f"no such member: {subject}"}
            if not target or not access.can_access_member(actor, target):
                return {"error": "you can only save member-private notes about yourself"}
            subject = target
        elif scope == "general" and not actor.is_admin:
            scope = "member"
            subject = actor.member_slug
        memory_id = db.add_memory(
            tool_input["note"],
            scope=scope,
            subject=subject,
            source=request.speaker,
            source_user_id=request.speaker_user_id,
            source_message_id=request.source_message_id,
        )
        return {"saved": True, "id": memory_id, "scope": scope}
    if name == "recall":
        subject = tool_input.get("subject")
        target = subject
        if subject and subject != "club":
            target = _member_slug(subject)
            if not target:
                return {"error": f"no such member: {subject}"}
            if not access.can_access_member(actor, target):
                return {"error": "another member's private memories are unavailable"}
        return model_readers.memories(actor=actor, subject=target, query=tool_input.get("query"))
    if name == "set_reminder":
        reminder_id = db.add_reminder(
            tool_input["due"],
            tool_input["text"],
            channel_id=request.channel_id,
            created_by=request.speaker,
        )
        return {"saved": True, "id": reminder_id}
    raise KeyError(name)
