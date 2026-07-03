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

from agent import clubdb
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
        "description": "Compact snapshot of Oliver's current operating context: current members, identity links, next meeting attendance status, high-level corpus stats, and recent feedback.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "current_meeting_status",
        "description": "The source of truth for the NEXT meeting: its canonical date, the book, and the picker, plus roll-call status under club rules (last Tuesday, quorum of 3 of 5 current members, picker must attend). Call this to verify any meeting date/time/book a member states before agreeing to it. Read-only.",
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
        "description": "Keyword-search everything you've said or heard across ALL mediums — Discord "
                       "channels (#ask-oliver, #general, #book-talk) AND your 1:1 email threads AND "
                       "the mailing list — newest first. This is your own conversation memory, NOT "
                       "the book corpus. Reach for it whenever someone refers to an earlier exchange "
                       "on any medium ('the books we went over in email', 'didn't we talk about…', "
                       "'what did someone say in book-talk'). Each result is tagged with its medium "
                       "(email / mailing list / Discord), who said it, and whether it was a member's "
                       "turn or YOUR reply — so you can tell email from Discord and see what you "
                       "yourself sent. Pass `member` to scope to one person's history with you across "
                       "mediums. For facts about books the club has read, use find_books/get_book.",
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
        "description": "Keyword-search the R/W Book Club email archive — the mailing list, archived "
                       "inbound email, AND Oliver's own sent replies (both sides of a thread). Use "
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
                       "returned by search_mail_archive — both the members' messages and Oliver's "
                       "own replies, in order.",
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
                                "enum": ["nomination", "comparison", "objection", "recommendation",
                                         "side_reference", "joke", "pick_candidate"]},
            },
            "required": ["title", "reason"],
        },
    },
    {
        "name": "book_cloud_recent",
        "description": "Read the club's private Book Cloud — books members have referenced but "
                       "(usually) not read. Default: raw recent mentions, newest first. Pass "
                       "titles=true for the aggregated orbit view (one row per title: first/last "
                       "mention, who, how often, recent reasons). Frame results as books orbiting "
                       "the conversation — not a queue, ranking, or commitment.",
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


def _surface_from_ctx(ctx: dict) -> str:
    channel = str(ctx.get("channel_id") or "")
    return ("mailing_list" if channel.startswith("email:list:")
            else "email" if channel.startswith("email:") else "discord")


def _member_lenses() -> dict:
    """Every current member's taste lens: reflection memories + recent picks. The who-will-love-it
    / who-will-fight-it evidence for pick advice — reactions must come from HERE, never invented."""
    lenses = {}
    for m in cr.human_current_members():  # taste lenses are human — Oliver has no vote here
        slug = m["slug"]
        hist = cr.member_history(slug) or {}
        lenses[slug] = {
            "name": m.get("name"),
            # The FULL active memory set, not a newest-N window: after archive mining, a member's
            # decisive note (e.g. Loren's Harari skepticism) can be years old — a partial lens
            # produced a live forecast that contradicted recorded taste.
            "memories": [x["note"] for x in db.get_memories(subject=slug, limit=40)],
            "recentPicks": [{"title": p.get("title"), "year": p.get("year")}
                            for p in (hist.get("picks") or [])[:3]],
        }
    return lenses


def _pick_fit(tool_input: dict, ctx: dict) -> dict:
    """One-call evaluation dossier for a pick candidate. Read-only except one Book Cloud INSERT
    recording that the candidate was considered (so evaluations enrich the cloud)."""
    from agent.enrich import openlibrary as enrich_ol

    title = tool_input["title"].strip()
    author = (tool_input.get("author") or "").strip() or None
    out: dict = {"candidate": {"title": title, "authors": [author] if author else [],
                               "resolved": "unresolved"}}
    subjects: list[str] = []

    corpus_hit = cr.find_book(title)
    if corpus_hit:
        subjects = corpus_hit.get("subjects") or []
        out["candidate"] = {
            "title": corpus_hit.get("title"), "authors": corpus_hit.get("authors") or [],
            "year": corpus_hit.get("year"), "subjects": subjects, "resolved": "corpus",
        }
        if corpus_hit.get("isRead"):
            out["alreadyRead"] = {  # re-picks are a special headline case
                "yearRead": (corpus_hit.get("meetingDate") or "")[:4],
                "picker": corpus_hit.get("pickerName"),
                "reviewSummary": cr.review_summary(corpus_hit["slug"]),
            }
        else:
            out["alreadyScheduled"] = {"meetingDate": corpus_hit.get("meetingDate"),
                                       "picker": corpus_hit.get("pickerName")}
    else:
        try:
            doc = enrich_ol.search_best_match(title, [author] if author else [])
        except Exception:  # noqa: BLE001 — OL down must not sink the dossier
            doc = None
        if doc:
            subjects = enrich_ol.clean_subjects(doc.get("subject"))
            out["candidate"] = {
                "title": doc.get("title") or title,
                "authors": doc.get("author_name") or ([author] if author else []),
                "year": doc.get("first_publish_year"),
                "pages": doc.get("number_of_pages_median"),
                "subjects": subjects,
                "ratingsAverage": doc.get("ratings_average"),
                "ratingsCount": doc.get("ratings_count"),
                "olKey": doc.get("key"),
                "resolved": "openlibrary",
            }

    cand_authors = out["candidate"].get("authors") or []
    # This title's own cloud history — who floated it, when, why.
    norm = title.lower().strip()
    out["cloudHistory"] = next(
        (r for r in db.book_cloud_titles(query=title, limit=5)
         if (r.get("title") or "").lower().strip() == norm), None)
    # Nearest neighbors on the shelf, each with the club's actual verdict.
    neighbors = cr.affinity_to_history(subjects, cand_authors, title=title)
    for n in neighbors:
        rs = cr.review_summary(n["slug"]) or {}
        n["clubVerdict"] = {"ratingAverage": rs.get("ratingAverage"),
                            "discussionAverage": rs.get("discussionAverage"),
                            "dnfCount": rs.get("dnfCount"),
                            "excerpt": (rs.get("excerpts") or [None])[0]}
    out["nearestInHistory"] = neighbors
    out["memberLenses"] = _member_lenses()
    stats = cr.club_stats()
    out["coverage"] = {"topics": stats.get("topics"), "fiction": stats.get("fiction"),
                       "nonfiction": stats.get("nonfiction")}
    out["clubLore"] = [x["note"] for x in db.get_memories(scope="club", limit=17)]
    out["note"] = ("Current reception/adaptation news is NOT included — web_search it. Never "
                   "state a member reaction that isn't grounded in memberLenses.")
    if "alreadyRead" not in out:  # considering a candidate enriches the cloud
        who = ctx.get("member_slug")
        db.add_book_cloud_entry(
            title=out["candidate"].get("title") or title,
            author=(cand_authors or [None])[0],
            book_slug=(corpus_hit or {}).get("slug"),
            reason=f"evaluated as a pick candidate{' for ' + who if who else ''}",
            reason_kind="pick_candidate",
            mentioned_by=who, mentioned_by_name=ctx.get("speaker"),
            surface=_surface_from_ctx(ctx), channel_id=ctx.get("channel_id"),
            source_message_id=ctx.get("source_message_id"),
        )
    return out


def _pick_prospects(tool_input: dict, ctx: dict) -> dict:
    """One-call discovery dossier: where should this member even look for a pick?"""
    member = (tool_input.get("member") or ctx.get("member_slug") or "").strip() or None
    direction = (tool_input.get("direction") or "").strip() or None
    out: dict = {"member": member, "direction": direction}

    if member:
        hist = cr.member_history(member) or {}
        out["memberTaste"] = {
            "memories": [x["note"] for x in db.get_memories(subject=member, limit=12)],
            # Their reviews across ALL reads (incl. others' picks) — a richer taste signal
            # than their own picks alone.
            "reviews": [{"book": r.get("book"), "rating": r.get("rating"), "dnf": r.get("dnf"),
                         "wouldRecommend": r.get("wouldRecommend")}
                        for r in (hist.get("reviews") or [])[:12]],
            "recentPicks": [{"title": p.get("title"), "year": p.get("year")}
                            for p in (hist.get("picks") or [])[:5]],
        }

    # The cloud's unread orbit — titles floated but never read, theirs vs the club's.
    read_slugs = {b["slug"] for b in cr.books() if b.get("isRead")}
    unread = [r for r in db.book_cloud_titles(limit=60)
              if not (r.get("book_slug") and r["book_slug"] in read_slugs)]
    out["cloudProspects"] = {
        "yours": [r for r in unread if member and member in (r.get("mentioners") or [])][:12],
        "clubOrbit": [r for r in unread
                      if not (member and member in (r.get("mentioners") or []))][:12],
        "totalUnreadInCloud": len(unread),
    }
    out["lovedAuthorsUnread"] = cr.unread_notable_works(limit=8)
    stats = cr.club_stats()
    topics_sorted = sorted(stats.get("topics") or [], key=lambda t: t[1])
    out["coverageGaps"] = {"leastReadTopics": topics_sorted[:5],
                           "fiction": stats.get("fiction"), "nonfiction": stats.get("nonfiction")}
    # Direction leads: when the member has said where they want to go, the fresh-search angles
    # for that lane come FIRST — the cloud/author leads are supporting color, not the shortlist.
    angles: list[str] = []
    if direction:
        angles += [
            f"best acclaimed {direction} books 2024 2025 2026",
            f"award-winning {direction} books recent",
            f"{direction} book accessible deep exploration general readers",
        ]
    angles += [f"notable acclaimed {t} books 2024 2025 2026" for t, _n in topics_sorted[:2]]
    angles += [f"new book by {a['author']} 2025 2026"
               for a in (out["lovedAuthorsUnread"] or [])[:2]]
    out["searchAngles"] = angles
    out["note"] = (
        ("The direction drives: web_search the direction angles FIRST for fresh, never-mentioned "
         "candidates — cloudProspects and lovedAuthorsUnread are supporting color, only where "
         "they fit the direction. Then pick_fit the best 2-3."
         if direction else
         "These are leads, not results — fresh candidates via web_search are fully in scope and "
         "often the best answer; web_search the angles, then pick_fit the best 2-3."))
    return out


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
        if name == "club_lists":
            return _dump([x for x in cr.lists() if x.get("scope") == "club"])
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
            sms_linked = {r["member_slug"] for r in db.list_member_sms()}
            website_linked = {r["member_slug"] for r in db.list_member_websites()}
            current = cr.human_current_members()
            return _dump({
                "speakerUserId": ctx.get("speaker_user_id"),
                "speakerMemberSlug": member_slug,
                "speakerMember": cr.find_member(member_slug) if member_slug else None,
                "linkedCurrentMembers": sorted(linked),
                "emailLinkedCurrentMembers": sorted(email_linked),
                "smsLinkedCurrentMembers": sorted(sms_linked),
                "websiteLinkedCurrentMembers": sorted(website_linked),
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
            rows = db.search_conversations(
                tool_input["query"], limit=limit, member_slug=tool_input.get("member") or None)
            out = []
            for r in rows:
                try:
                    channel_key = int(r["channel_id"])
                except (TypeError, ValueError):
                    channel_key = r["channel_id"]
                out.append({
                    "medium": db.conversation_medium(r["channel_id"]),
                    "channel": config.CHANNEL_NAMES.get(channel_key, r["channel_id"]),
                    "who": r.get("speaker"),
                    "member": r.get("member_slug"),
                    # role tells Oliver whether this was a member's turn or its OWN reply.
                    "role": r["role"],
                    "when": r.get("created_at"),
                    "content": (r["content"] or "")[:300],  # keep tool result compact
                })
            return _dump(out)
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
        if name == "club_timeline":
            limit = max(1, min(int(tool_input.get("limit", 30)), 100))
            member_id = None
            if tool_input.get("member"):
                m = cr.find_member(tool_input["member"])
                member_id = clubdb.lookup_member_id(m["slug"]) if m else None
                if member_id is None:
                    return _dump({"error": f"no such member: {tool_input['member']}"})
            rows = db.timeline(
                category=tool_input.get("category"),
                member_id=member_id,
                since=tool_input.get("since"),
                until=tool_input.get("until"),
                limit=limit,
            )
            out = [
                {"date": (r.get("occurred_at") or "")[:10], "category": r["category"],
                 "kind": r["kind"], "member": r.get("member_slug"),
                 "detail": (r.get("detail") or "")[:500], "source": r.get("source")}
                for r in rows
            ]
            return _dump(out)
        if name == "record_timeline_event":
            category = tool_input.get("category")
            kind = tool_input.get("kind")
            if kind not in (db.CHRONICLE_KINDS.get(category) or ()):
                return _dump({"error": f"kind {kind!r} is not valid for category {category!r}; "
                                       f"allowed: {db.CHRONICLE_KINDS.get(category)}"})
            member_id = None
            member_slug = None
            if tool_input.get("member"):
                m = cr.find_member(tool_input["member"])
                if not m:
                    return _dump({"error": f"no such member: {tool_input['member']}"})
                member_slug = m["slug"]
                member_id = clubdb.lookup_member_id(member_slug)
            surface = "email" if str(ctx.get("speaker_user_id") or "").startswith("email:") else "discord"
            eid = db.record_event(
                actor="oliver",
                surface=surface,
                kind=kind,
                category=category,
                member_id=member_id,
                detail={"summary": tool_input.get("summary"),
                        "members": [member_slug] if member_slug else []},
                occurred_at=tool_input.get("date"),
            )
            db.add_activity(
                "timeline_event",
                "Timeline event recorded",
                f"Category: {category}\nKind: {kind}\nDate: {tool_input.get('date')}\n"
                f"Member: {member_slug or '(club-wide)'}\nSummary: {tool_input.get('summary')}",
            )
            return _dump({"saved": True, "id": eid, "category": category, "kind": kind})
        if name == "record_availability":
            member_slug = ctx.get("member_slug")
            if not member_slug:
                return _dump({"error": "speaker is not linked to a club member"})
            member_id = clubdb.lookup_member_id(member_slug)
            status = tool_input["status"]
            meeting = meeting_rules.next_meeting()
            meeting_id = meeting["meetingId"]
            if meeting_id is None or member_id is None:
                return _dump({"error": "no scheduled meeting to record availability against"})
            db.record_attendance_report(
                meeting_id,
                member_id,
                status,
                surface=("email" if str(ctx.get("speaker_user_id") or "").startswith("email:") else "discord"),
                updated_by=ctx.get("speaker_user_id"),
            )
            db.add_activity(
                "roll_call_update",
                "Roll-call response recorded",
                f"Member: {member_slug}\nStatus: {status}\nMeeting: {meeting['meetingKey']}",
            )
            return _dump({"saved": True, "meetingStatus": meeting_rules.meeting_status(meeting_id)})
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
        if name == "book_cloud_add":
            # Mentioner/surface/provenance come from ctx, never model input (identity map rule).
            surface = _surface_from_ctx(ctx)
            entry_id = db.add_book_cloud_entry(
                title=tool_input["title"],
                reason=tool_input["reason"],
                author=tool_input.get("author"),
                reason_kind=tool_input.get("reason_kind"),
                book_slug=(cr.find_book(tool_input["title"]) or {}).get("slug"),
                mentioned_by=ctx.get("member_slug"),
                mentioned_by_name=ctx.get("speaker"),
                surface=surface,
                channel_id=ctx.get("channel_id"),
                source_message_id=ctx.get("source_message_id"),
            )
            return _dump({"saved": True, "id": entry_id})
        if name == "book_cloud_recent":
            limit = max(1, min(int(tool_input.get("limit", 20)), 50))
            if tool_input.get("titles"):
                return _dump(db.book_cloud_titles(query=tool_input.get("query"),
                                                  member=tool_input.get("member"), limit=limit))
            return _dump(db.recent_book_cloud(limit=limit, query=tool_input.get("query")))
        if name == "pick_fit":
            return _dump(_pick_fit(tool_input, ctx))
        if name == "pick_prospects":
            return _dump(_pick_prospects(tool_input, ctx))
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
            member_id = clubdb.lookup_member_id(member_slug)
            meeting = meeting_rules.next_meeting()
            meeting_id = meeting["meetingId"]
            if meeting_id is None or member_id is None:
                return _dump({"error": "no scheduled meeting to record reading status against"})
            db.record_reading_report(
                meeting_id,
                member_id,
                tool_input["status"],
                progress=tool_input.get("progress"),
                page=tool_input.get("page"),
                percent=tool_input.get("percent"),
                surface=("email" if str(ctx.get("speaker_user_id") or "").startswith("email:") else "discord"),
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
            meeting_id = meeting["meetingId"]
            member_id = clubdb.lookup_member_id(member["slug"])
            if meeting_id is None or member_id is None:
                return _dump({"error": "no scheduled meeting to check in against"})
            book = meeting.get("book") or {}
            title = book.get("title") or "the current book"
            existing = db.meeting_member_status(meeting_id, member_id)
            if existing and existing["reading"] == "finished":
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
            body = meeting_rules.reading_checkin_email_body(
                member["name"], meeting, note=tool_input.get("note"))
            subject = f"Reading check-in: {title}"
            sent = outbound.send(to=[email["email"]], subject=subject, body=body)
            db.record_reading_request(meeting_id, member_id, surface="email")
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
                    cr.human_current_members(),
                    key=lambda m: m.get("name") or m["slug"],
                )
            status = meeting_rules.meeting_status()
            meeting = status["meeting"]
            meeting_id = meeting["meetingId"]
            if meeting_id is None:
                return _dump({"error": "no scheduled meeting to run roll call against"})
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
                member_id = clubdb.lookup_member_id(member["slug"])
                if member_id is None:
                    missing.append({"member": member["slug"], "reason": "not in the club database"})
                    continue
                subject = _roll_call_subject(status)
                body = _roll_call_email_body(member.get("name") or member["slug"], status, note=note)
                sent = outbound.send(to=[email["email"]], subject=subject, body=body)
                db.record_attendance_request(meeting_id, member_id, actor="oliver", surface="email")
                db.add_activity(
                    "email_sent",
                    "Roll-call email sent",
                    f"Member: {member['slug']}\nTo: {email['email']}\nSubject: {subject}\nEmail ID: {sent.get('emailId')}",
                )
                sent_rows.append({"member": member["slug"], **sent})
            if sent_rows and not db.has_open_roll_call(meeting_id):
                db.record_group_event(
                    meeting_id,
                    "roll_call_opened",
                    actor="oliver",
                    detail={"channel_id": ctx.get("channel_id"), "opened_by": "email-tool"},
                )
            return _dump({
                "sent": sent_rows,
                "skipped": skipped,
                "missing": missing,
                "meetingStatus": meeting_rules.meeting_status(meeting_id),
            })
        return _dump({"error": f"unknown tool {name}"})
    except Exception as e:  # noqa: BLE001 - surface tool errors to the model, don't crash the loop
        # Also log so the operator sees it — bare error strings to the model
        # used to be invisible to anyone watching the bot.
        log.exception("tool %s failed (input=%r)", name, tool_input)
        return _dump({"error": f"{type(e).__name__}: {e}"})


def _reading_status_snapshot(meeting: dict) -> dict:
    meeting_id = meeting.get("meetingId")
    rows = {
        r["member_slug"]: r
        for r in (db.meeting_member_status_for_meeting(meeting_id) if meeting_id is not None else [])
    }
    members = cr.human_current_members()
    statuses = []
    for member in sorted(members, key=lambda m: m.get("name") or m["slug"]):
        row = rows.get(member["slug"])
        statuses.append({
            "member": member.get("name"),
            "memberSlug": member["slug"],
            "status": row["reading"] if row else "unknown",
            "progress": row.get("reading_progress") if row else None,
            "page": row.get("reading_page") if row else None,
            "percent": row.get("reading_percent") if row else None,
            "updatedAt": row.get("reading_answered_at") if row else None,
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


# Roll-call email text is shared with the command path; it lives in meeting_rules so the
# wording can't drift between the two senders.
_roll_call_subject = meeting_rules.roll_call_subject
_roll_call_email_body = meeting_rules.roll_call_email_body
