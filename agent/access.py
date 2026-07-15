"""Server-enforced actor and tool-access policy for Oliver.

The prompt explains privacy to the model, but the dispatcher is the authority.  Public corpus
lookups remain available to any speaker; club-operational/private tools require a linked member;
and repair/audit surfaces require the configured admin.  Data readers apply their own row-level
scope as a second boundary (a member can see shared club material and their own private material).
"""

from __future__ import annotations

from dataclasses import dataclass

from agent import config, identities

# These tools expose club-operational or private state, or create durable/outward effects.  Keeping
# the list here makes ambient model authority reviewable in one place instead of scattered across
# prompt prose and dispatch branches.
MEMBER_ONLY_TOOLS = frozenset({
    "pending_reviews",
    "current_club_state",
    "current_meeting_status",
    "meeting_readiness",
    "identity_status",
    "recent_channel_context",
    "record_availability",
    "propose_action",
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
})

ADMIN_ONLY_TOOLS = frozenset({
    "recent_feedback",
    "open_proposals",
    "meeting_campaign",
})


@dataclass(frozen=True)
class Actor:
    """Identity resolved by trusted runtime context, never by model input."""

    member_slug: str | None
    is_admin: bool

    @property
    def linked(self) -> bool:
        return bool(self.member_slug)


def actor_from_ctx(ctx: dict) -> Actor:
    member_slug = (ctx.get("member_slug") or "").strip() or None
    speaker_user_id = str(ctx.get("speaker_user_id") or "")
    admin_user_id = str(config.ADMIN_USER_ID) if config.ADMIN_USER_ID else ""

    is_admin = bool(admin_user_id and speaker_user_id == admin_user_id)
    # Identity is unified across Discord/email.  Once the configured Discord admin is linked to a
    # member, that same member's allowlisted email is an admin surface too.
    if not is_admin and admin_user_id and member_slug:
        is_admin = identities.member_slug_for_user(admin_user_id) == member_slug
    return Actor(member_slug=member_slug, is_admin=is_admin)


def tool_access_error(name: str, actor: Actor) -> str | None:
    if name in ADMIN_ONLY_TOOLS and not actor.is_admin:
        return "this tool is available only to the club admin"
    if name in MEMBER_ONLY_TOOLS and not actor.linked:
        return "this tool requires a linked club-member identity"
    return None


def can_access_member(actor: Actor, member_slug: str) -> bool:
    """Whether actor may read/write member-private state for ``member_slug``."""
    return actor.is_admin or bool(actor.member_slug and actor.member_slug == member_slug)


def is_shared_conversation(channel_id: str | None) -> bool:
    """Discord + mailing-list conversations are shared; 1:1 email is member-private."""
    channel = str(channel_id or "")
    return not channel.startswith("email:") or channel.startswith("email:list:")
