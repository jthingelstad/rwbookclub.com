"""Public corpus and Oliver operational tool handlers."""

from __future__ import annotations

from agent import corpus_read as cr
from agent import db, identities
from agent.tool_handlers.context import RequestContext

NAMES = frozenset(
    {
        "find_books",
        "search_books",
        "get_book",
        "related_books",
        "compare_books",
        "review_summary",
        "member_history",
        "upcoming_meetings",
        "get_author",
        "club_lists",
        "club_stats",
        "identity_status",
        "recent_feedback",
        "propose_action",
        "open_proposals",
    }
)


def handle(name: str, tool_input: dict, request: RequestContext):
    if name == "find_books":
        return cr.find_books(tool_input["query"])
    if name == "search_books":
        return cr.search_books(**tool_input)
    if name == "get_book":
        return cr.get_book(tool_input["book"]) or {"error": "no such book"}
    if name == "related_books":
        limit = max(1, min(int(tool_input.get("limit", 8)), 12))
        return cr.related_books(tool_input["book"], limit=limit) or {"error": "no such book"}
    if name == "compare_books":
        return cr.compare_books(tool_input["books"])
    if name == "review_summary":
        return cr.review_summary(tool_input["book"]) or {"error": "no such book"}
    if name == "member_history":
        return cr.member_history(tool_input["member"]) or {"error": "no such member"}
    if name == "upcoming_meetings":
        return cr.upcoming_meetings()
    if name == "get_author":
        return cr.get_author(tool_input["author"]) or {"error": "no such author"}
    if name == "club_lists":
        return [item for item in cr.lists() if item.get("scope") == "club"]
    if name == "club_stats":
        return cr.club_stats()
    if name == "identity_status":
        member_slug = request.member_slug
        linked = {row["member_slug"] for row in identities.list_member_identities()}
        email_linked = {row["member_slug"] for row in identities.list_member_emails()}
        sms_linked = {row["member_slug"] for row in identities.list_member_sms()}
        website_linked = {row["member_slug"] for row in identities.list_member_websites()}
        current = cr.human_current_members()
        if not request.actor.is_admin:
            return {
                "speakerUserId": request.speaker_user_id,
                "speakerMemberSlug": member_slug,
                "speakerMember": cr.find_member(member_slug) if member_slug else None,
                "discordLinked": member_slug in linked,
                "emailLinked": member_slug in email_linked,
                "smsLinked": member_slug in sms_linked,
                "websiteLinked": member_slug in website_linked,
            }
        return {
            "speakerUserId": request.speaker_user_id,
            "speakerMemberSlug": member_slug,
            "speakerMember": cr.find_member(member_slug) if member_slug else None,
            "linkedCurrentMembers": sorted(linked),
            "emailLinkedCurrentMembers": sorted(email_linked),
            "smsLinkedCurrentMembers": sorted(sms_linked),
            "websiteLinkedCurrentMembers": sorted(website_linked),
            "missingCurrentMembers": [
                {"slug": member["slug"], "name": member.get("name")}
                for member in current
                if member["slug"] not in linked
            ],
            "missingEmailCurrentMembers": [
                {"slug": member["slug"], "name": member.get("name")}
                for member in current
                if member["slug"] not in email_linked
            ],
        }
    if name == "recent_feedback":
        return db.feedback_stats()
    if name == "propose_action":
        proposal_id = db.add_proposal(
            kind=tool_input["kind"],
            title=tool_input["title"],
            body=tool_input["body"],
            channel_id=request.channel_id,
            source_user_id=request.speaker_user_id,
        )
        return {"saved": True, "id": proposal_id}
    if name == "open_proposals":
        limit = max(1, min(int(tool_input.get("limit", 10)), 10))
        return db.list_proposals(limit=limit)
    raise KeyError(name)
