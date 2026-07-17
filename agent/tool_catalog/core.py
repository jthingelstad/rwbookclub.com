"""Public corpus and Oliver operational tool contracts."""

TOOLS = [
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
            "properties": {
                "query": {
                    "type": "string",
                    "description": "free-text — a topic, theme, author name, or phrase",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_books",
        "description": "Precise filter-based browse — use when you want to LIST everything "
        "matching specific criteria (all 2018 reads, all Technology books, "
        "all sci-fi). Filters work alone — omit `query` for a pure filter "
        'browse. For vague "do we have anything about X" questions, use '
        "find_books instead.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "free-text substring match on title/subtitle/synopsis/author/topic. Optional — omit for filter-only browse.",
                },
                "topic": {
                    "type": "string",
                    "description": "exact topic category, e.g. 'Technology'",
                },
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
        "name": "propose_action",
        "description": "Stage a non-destructive proposal for admins to review later. Use for suggested corpus patches, reading-order concerns, review nudges, memory repairs, meeting notices, or other club operations that should not be executed directly.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": [
                        "corpus_patch",
                        "reading_order",
                        "review_nudge",
                        "memory_update",
                        "meeting_notice",
                        "other",
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
]
