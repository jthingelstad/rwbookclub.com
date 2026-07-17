"""Mail archive and outbound email tool contracts."""

TOOLS = [
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
                "query": {
                    "type": "string",
                    "description": "words to search for; all terms must match",
                },
                "member": {
                    "type": "string",
                    "description": "Optional member slug, e.g. jamie, tom, erik",
                },
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
]
