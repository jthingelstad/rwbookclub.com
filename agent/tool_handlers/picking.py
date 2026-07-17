"""Book Cloud writes/reads and private pick-guidance capabilities."""

from __future__ import annotations

from agent import access, db, model_readers
from agent import corpus_read as cr
from agent.tool_handlers.context import RequestContext

NAMES = frozenset({"book_cloud_add", "book_cloud_recent", "pick_fit", "pick_prospects"})


def _member_slug(value: str | None) -> str | None:
    member = cr.find_member(value) if value else None
    return member.get("slug") if member else None


def member_lenses(request: RequestContext) -> dict:
    """Current members' public pick history plus only actor-visible private taste notes."""
    lenses = {}
    for member in cr.human_current_members():
        slug = member["slug"]
        history = cr.member_history(slug) or {}
        lenses[slug] = {
            "name": member.get("name"),
            "memories": (
                [
                    row["note"]
                    for row in model_readers.memories(actor=request.actor, subject=slug, limit=40)
                ]
                if access.can_access_member(request.actor, slug)
                else []
            ),
            "recentPicks": [
                {"title": pick.get("title"), "year": pick.get("year")}
                for pick in (history.get("picks") or [])[:3]
            ],
        }
    return lenses


def pick_fit(tool_input: dict, request: RequestContext) -> dict:
    from agent.enrich import openlibrary as enrich_ol

    title = tool_input["title"].strip()
    author = (tool_input.get("author") or "").strip() or None
    out: dict = {
        "candidate": {
            "title": title,
            "authors": [author] if author else [],
            "resolved": "unresolved",
        }
    }
    subjects: list[str] = []

    corpus_hit = cr.find_book(title)
    if corpus_hit:
        subjects = corpus_hit.get("subjects") or []
        out["candidate"] = {
            "title": corpus_hit.get("title"),
            "authors": corpus_hit.get("authors") or [],
            "year": corpus_hit.get("year"),
            "subjects": subjects,
            "resolved": "corpus",
        }
        if corpus_hit.get("isRead"):
            out["alreadyRead"] = {
                "yearRead": (corpus_hit.get("meetingDate") or "")[:4],
                "picker": corpus_hit.get("pickerName"),
                "reviewSummary": cr.review_summary(corpus_hit["slug"]),
            }
        else:
            out["alreadyScheduled"] = {
                "meetingDate": corpus_hit.get("meetingDate"),
                "picker": corpus_hit.get("pickerName"),
            }
    else:
        try:
            document = enrich_ol.search_best_match(title, [author] if author else [])
        except Exception:
            document = None
        if document:
            subjects = enrich_ol.clean_subjects(document.get("subject"))
            out["candidate"] = {
                "title": document.get("title") or title,
                "authors": document.get("author_name") or ([author] if author else []),
                "year": document.get("first_publish_year"),
                "pages": document.get("number_of_pages_median"),
                "subjects": subjects,
                "ratingsAverage": document.get("ratings_average"),
                "ratingsCount": document.get("ratings_count"),
                "olKey": document.get("key"),
                "resolved": "openlibrary",
            }

    candidate_authors = out["candidate"].get("authors") or []
    normalized_title = title.lower().strip()
    out["cloudHistory"] = next(
        (
            row
            for row in model_readers.book_cloud_titles(actor=request.actor, query=title, limit=5)
            if (row.get("title") or "").lower().strip() == normalized_title
        ),
        None,
    )
    neighbors = cr.affinity_to_history(subjects, candidate_authors, title=title)
    for neighbor in neighbors:
        summary = cr.review_summary(neighbor["slug"]) or {}
        neighbor["clubVerdict"] = {
            "ratingAverage": summary.get("ratingAverage"),
            "discussionAverage": summary.get("discussionAverage"),
            "dnfCount": summary.get("dnfCount"),
            "excerpt": (summary.get("excerpts") or [None])[0],
        }
    out["nearestInHistory"] = neighbors
    out["memberLenses"] = member_lenses(request)
    stats = cr.club_stats()
    out["coverage"] = {
        "topics": stats.get("topics"),
        "fiction": stats.get("fiction"),
        "nonfiction": stats.get("nonfiction"),
    }
    out["clubLore"] = [
        row["note"] for row in model_readers.memories(actor=request.actor, subject="club", limit=17)
    ]
    out["note"] = (
        "Current reception/adaptation news is NOT included — web_search it. Never "
        "state a member reaction that isn't grounded in memberLenses."
    )
    if "alreadyRead" not in out:
        db.add_book_cloud_entry(
            title=out["candidate"].get("title") or title,
            author=(candidate_authors or [None])[0],
            book_slug=(corpus_hit or {}).get("slug"),
            reason=(
                "evaluated as a pick candidate"
                + (f" for {request.member_slug}" if request.member_slug else "")
            ),
            reason_kind="pick_candidate",
            mentioned_by=request.member_slug,
            mentioned_by_name=request.speaker,
            surface=request.surface,
            channel_id=request.channel_id,
            source_message_id=request.source_message_id,
        )
    return out


def pick_prospects(tool_input: dict, request: RequestContext) -> dict:
    requested = tool_input.get("member")
    member = _member_slug(requested) if requested else request.member_slug
    if requested and not member:
        return {"error": f"no such member: {requested}"}
    if member and not access.can_access_member(request.actor, member):
        return {"error": "private pick guidance can only use your own member profile"}
    direction = (tool_input.get("direction") or "").strip() or None
    out: dict = {"member": member, "direction": direction}

    if member:
        history = cr.member_history(member) or {}
        out["memberTaste"] = {
            "memories": [
                row["note"]
                for row in model_readers.memories(actor=request.actor, subject=member, limit=12)
            ],
            "reviews": [
                {
                    "book": row.get("book"),
                    "rating": row.get("rating"),
                    "dnf": row.get("dnf"),
                    "wouldRecommend": row.get("wouldRecommend"),
                }
                for row in (history.get("reviews") or [])[:12]
            ],
            "recentPicks": [
                {"title": row.get("title"), "year": row.get("year")}
                for row in (history.get("picks") or [])[:5]
            ],
        }

    read_slugs = {book["slug"] for book in cr.books() if book.get("isRead")}
    unread = [
        row
        for row in model_readers.book_cloud_titles(actor=request.actor, limit=60)
        if not (row.get("book_slug") and row["book_slug"] in read_slugs)
    ]
    out["cloudProspects"] = {
        "yours": [row for row in unread if member and member in (row.get("mentioners") or [])][:12],
        "clubOrbit": [
            row for row in unread if not (member and member in (row.get("mentioners") or []))
        ][:12],
        "totalUnreadInCloud": len(unread),
    }
    out["lovedAuthorsUnread"] = cr.unread_notable_works(limit=8)
    stats = cr.club_stats()
    topics_sorted = sorted(stats.get("topics") or [], key=lambda topic: topic[1])
    out["coverageGaps"] = {
        "leastReadTopics": topics_sorted[:5],
        "fiction": stats.get("fiction"),
        "nonfiction": stats.get("nonfiction"),
    }
    angles: list[str] = []
    if direction:
        angles += [
            f"best acclaimed {direction} books 2024 2025 2026",
            f"award-winning {direction} books recent",
            f"{direction} book accessible deep exploration general readers",
        ]
    angles += [
        f"notable acclaimed {topic} books 2024 2025 2026" for topic, _count in topics_sorted[:2]
    ]
    angles += [
        f"new book by {row['author']} 2025 2026" for row in (out["lovedAuthorsUnread"] or [])[:2]
    ]
    out["searchAngles"] = angles
    out["note"] = (
        "The direction drives: web_search the direction angles FIRST for fresh, never-mentioned "
        "candidates — cloudProspects and lovedAuthorsUnread are supporting color, only where "
        "they fit the direction. Then pick_fit the best 2-3."
        if direction
        else "These are leads, not results — fresh candidates via web_search are fully in scope and "
        "often the best answer; web_search the angles, then pick_fit the best 2-3."
    )
    return out


def handle(name: str, tool_input: dict, request: RequestContext):
    if name == "book_cloud_add":
        entry_id = db.add_book_cloud_entry(
            title=tool_input["title"],
            reason=tool_input["reason"],
            author=tool_input.get("author"),
            reason_kind=tool_input.get("reason_kind"),
            book_slug=(cr.find_book(tool_input["title"]) or {}).get("slug"),
            mentioned_by=request.member_slug,
            mentioned_by_name=request.speaker,
            surface=request.surface,
            channel_id=request.channel_id,
            source_message_id=request.source_message_id,
        )
        return {"saved": True, "id": entry_id}
    if name == "book_cloud_recent":
        limit = max(1, min(int(tool_input.get("limit", 20)), 50))
        if tool_input.get("titles"):
            return model_readers.book_cloud_titles(
                actor=request.actor,
                query=tool_input.get("query"),
                member=tool_input.get("member"),
                limit=limit,
            )
        return model_readers.recent_book_cloud(
            actor=request.actor,
            query=tool_input.get("query"),
            member=tool_input.get("member"),
            limit=limit,
        )
    if name == "pick_fit":
        return pick_fit(tool_input, request)
    if name == "pick_prospects":
        return pick_prospects(tool_input, request)
    raise KeyError(name)
