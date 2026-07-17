"""Actor-scoped model readers.

Tool handlers import this module for private operational data. Raw/internal repair and admin code
uses :mod:`agent.db` directly; the APIs here require an Actor and always call row-scoped readers.
"""

from __future__ import annotations

from agent import access, db


def recent_channel(*, actor: access.Actor, channel_id: str, limit: int) -> list[dict]:
    del actor  # required by the boundary even though the channel itself supplies row scope
    return db.recent_messages(channel_id, limit=limit)


def search_discussion(
    *, actor: access.Actor, query: str, member_slug: str | None, limit: int
) -> list[dict]:
    return db.search_conversations_visible(
        query,
        limit=limit,
        member_slug=member_slug,
        viewer_member_slug=actor.member_slug,
        is_admin=actor.is_admin,
    )


def search_mail(
    *,
    actor: access.Actor,
    query: str,
    member_slug: str | None,
    year_from: int | None,
    year_to: int | None,
    limit: int,
) -> list[dict]:
    return db.search_mail_archive_visible(
        query,
        viewer_member_slug=actor.member_slug,
        is_admin=actor.is_admin,
        member_slug=member_slug,
        year_from=year_from,
        year_to=year_to,
        limit=limit,
    )


def mail_thread(*, actor: access.Actor, thread_id: str, limit: int) -> dict | None:
    return db.get_mail_thread_visible(
        thread_id,
        limit=limit,
        viewer_member_slug=actor.member_slug,
        is_admin=actor.is_admin,
    )


def memories(
    *, actor: access.Actor, subject: str | None, query: str | None = None, limit: int = 20
) -> list[dict]:
    return db.visible_memories(
        viewer_member_slug=actor.member_slug,
        is_admin=actor.is_admin,
        subject=subject,
        query=query,
        limit=limit,
    )


def book_cloud_titles(
    *, actor: access.Actor, query: str | None = None, member: str | None = None, limit: int = 20
) -> list[dict]:
    return db.book_cloud_titles_visible(
        viewer_member_slug=actor.member_slug,
        is_admin=actor.is_admin,
        query=query,
        member=member,
        limit=limit,
    )


def recent_book_cloud(
    *, actor: access.Actor, query: str | None = None, member: str | None = None, limit: int = 20
) -> list[dict]:
    return db.recent_book_cloud_visible(
        viewer_member_slug=actor.member_slug,
        is_admin=actor.is_admin,
        query=query,
        member=member,
        limit=limit,
    )
