"""Book Cloud and private pick-guidance tool contracts."""

TOOLS = [
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
                "reason": {
                    "type": "string",
                    "description": "Why it came up — the specific connection, comparison, or context.",
                },
                "author": {"type": "string"},
                "reason_kind": {
                    "type": "string",
                    "enum": [
                        "nomination",
                        "recommendation",
                        "comparison",
                        "caution",
                        "context",
                        "inquiry",
                        "joke",
                    ],
                },
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
                "query": {
                    "type": "string",
                    "description": "Optional filter over title/author/reason.",
                },
                "titles": {"type": "boolean", "description": "Aggregated one-row-per-title view."},
                "member": {
                    "type": "string",
                    "description": "With titles=true: only titles this member slug has mentioned.",
                },
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
                "member": {
                    "type": "string",
                    "description": "Member slug; defaults to whoever is asking.",
                },
                "direction": {
                    "type": "string",
                    "description": "Optional steer, e.g. 'fiction' or 'something in the history lane'.",
                },
            },
        },
    },
]
