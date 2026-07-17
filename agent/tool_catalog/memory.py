"""Memory, conversation retrieval, and reminder tool contracts."""

TOOLS = [
    {
        "name": "recent_channel_context",
        "description": "Recent Oliver-visible turns in this Discord channel. This is not the whole channel, just prior messages Oliver handled.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},
        },
    },
    {
        "name": "remember",
        "description": "Save a durable note Oliver should remember across conversations (a member's taste, a club fact, a preference). Private to Oliver.",
        "input_schema": {
            "type": "object",
            "properties": {
                "note": {"type": "string"},
                "subject": {
                    "type": "string",
                    "description": "who/what it's about, e.g. a member slug like 'nick'",
                },
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
                "due": {
                    "type": "string",
                    "description": "ISO 8601 datetime, e.g. 2026-06-25T17:00:00Z",
                },
                "text": {"type": "string"},
            },
            "required": ["due", "text"],
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
                "query": {
                    "type": "string",
                    "description": "words to look for; a turn must contain every word (AND match)",
                },
                "member": {
                    "type": "string",
                    "description": "Optional member slug (e.g. jamie) — scope to your conversations with that person across mediums",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
    },
]
