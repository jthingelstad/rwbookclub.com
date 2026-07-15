"""Meeting, attendance, reading, outreach, and timeline tool contracts."""

TOOLS = [
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
]

