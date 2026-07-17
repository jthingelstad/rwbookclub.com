"""Member identity and contact persistence.

This capability owns the normalized links between club members and their Discord, email, SMS, and
website identities. Email and mobile values remain private SQLite state; only validated website
URLs are projected to public member profiles.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlsplit

from agent import db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Storage is keyed by member_id (FK to club_members); the helpers keep a slug-based
# interface because callers build a 'member:<slug>' speaker string / look up the corpus
# member by slug. Slug↔id is resolved at this boundary, ids are the stored link.
def _member_id_for_slug(conn: sqlite3.Connection, slug: str | None) -> int | None:
    if not slug:
        return None
    r = conn.execute("SELECT id FROM club_members WHERE slug = ?", (slug,)).fetchone()
    return r["id"] if r else None


def _link_identity(
    surface: str,
    identifier: str,
    member_slug: str,
    *,
    is_primary: bool = False,
    linked_by: str | None = None,
    label: str | None = None,
) -> None:
    with db.connect() as conn:
        mid = _member_id_for_slug(conn, member_slug)
        if mid is None:
            raise ValueError(f"no club member with slug {member_slug!r}")
        conn.execute(
            "INSERT INTO member_identities (surface, identifier, member_id, is_primary, linked_by, label, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(surface, identifier) DO UPDATE SET "
            "member_id=excluded.member_id, is_primary=excluded.is_primary, linked_by=excluded.linked_by, "
            "label=COALESCE(excluded.label, member_identities.label), updated_at=excluded.updated_at",
            (
                surface,
                identifier,
                mid,
                1 if is_primary else 0,
                linked_by,
                (label or "").strip() or None,
                _now(),
            ),
        )


def member_handles(member_slug: str, surface: str) -> list[dict]:
    """A member's handles for a surface with their primary flag, primary-first."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT mi.identifier, mi.is_primary, mi.label FROM member_identities mi "
            "JOIN club_members m ON m.id = mi.member_id "
            "WHERE m.slug = ? AND mi.surface = ? ORDER BY mi.is_primary DESC, mi.identifier",
            (member_slug, surface),
        ).fetchall()
    return [
        {"identifier": r["identifier"], "is_primary": bool(r["is_primary"]), "label": r["label"]}
        for r in rows
    ]


def set_primary_identity(member_slug: str, surface: str, identifier: str) -> bool:
    """Mark one handle primary for (member, surface), clearing the flag on the others. Returns True
    if the identifier belongs to this member + surface."""
    with db.connect() as conn:
        mid = _member_id_for_slug(conn, member_slug)
        if mid is None:
            return False
        owned = conn.execute(
            "SELECT 1 FROM member_identities WHERE surface = ? AND identifier = ? AND member_id = ?",
            (surface, identifier, mid),
        ).fetchone()
        if not owned:
            return False
        conn.execute(
            "UPDATE member_identities SET is_primary = 0 WHERE surface = ? AND member_id = ?",
            (surface, mid),
        )
        conn.execute(
            "UPDATE member_identities SET is_primary = 1, updated_at = ? "
            "WHERE surface = ? AND identifier = ?",
            (_now(), surface, identifier),
        )
    return True


def link_member_identity(
    discord_user_id: str, member_slug: str, *, linked_by: str | None = None
) -> None:
    _link_identity("discord", discord_user_id, member_slug, linked_by=linked_by)


def member_slug_for_user(discord_user_id: str | None) -> str | None:
    if not discord_user_id:
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT m.slug FROM member_identities mi JOIN club_members m ON m.id = mi.member_id "
            "WHERE mi.surface = 'discord' AND mi.identifier = ?",
            (discord_user_id,),
        ).fetchone()
    return row["slug"] if row else None


def list_member_identities() -> list[dict]:
    """Discord links with member_slug projected (callers + admin display)."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT mi.identifier AS discord_user_id, m.slug AS member_slug, "
            "mi.linked_by, mi.created_at, mi.updated_at "
            "FROM member_identities mi JOIN club_members m ON m.id = mi.member_id "
            "WHERE mi.surface = 'discord' ORDER BY m.slug"
        ).fetchall()
    return [dict(r) for r in rows]


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def link_member_email(
    email: str, member_slug: str, *, linked_by: str | None = None, is_primary: bool = False
) -> None:
    email = _normalize_email(email)
    if not email or "@" not in email:
        raise ValueError("email must look like an email address")
    _link_identity("email", email, member_slug, is_primary=is_primary, linked_by=linked_by)


def member_slug_for_email(email: str | None) -> str | None:
    if not email:
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT m.slug FROM member_identities mi JOIN club_members m ON m.id = mi.member_id "
            "WHERE mi.surface = 'email' AND mi.identifier = ?",
            (_normalize_email(email),),
        ).fetchone()
    return row["slug"] if row else None


def email_for_member(member_slug: str) -> dict | None:
    """The member's primary email as {email, member_slug}, or None. Primary first."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT mi.identifier AS email, m.slug AS member_slug, mi.is_primary, mi.linked_by "
            "FROM member_identities mi JOIN club_members m ON m.id = mi.member_id "
            "WHERE mi.surface = 'email' AND m.slug = ? "
            "ORDER BY mi.is_primary DESC, mi.updated_at DESC LIMIT 1",
            (member_slug,),
        ).fetchone()
    return dict(row) if row else None


def emails_for_member(member_slug: str) -> list[str]:
    """All of a member's email addresses, primary first."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT mi.identifier AS email FROM member_identities mi "
            "JOIN club_members m ON m.id = mi.member_id "
            "WHERE mi.surface = 'email' AND m.slug = ? ORDER BY mi.is_primary DESC, mi.identifier",
            (member_slug,),
        ).fetchall()
    return [r["email"] for r in rows]


def list_member_emails() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT mi.identifier AS email, m.slug AS member_slug, mi.is_primary, "
            "mi.linked_by, mi.created_at, mi.updated_at "
            "FROM member_identities mi JOIN club_members m ON m.id = mi.member_id "
            "WHERE mi.surface = 'email' ORDER BY m.slug, mi.identifier"
        ).fetchall()
    return [dict(r) for r in rows]


def _normalize_phone(number: str) -> str:
    """Keep a leading '+' and digits only — a loose E.164-ish normal form for dedup."""
    number = number.strip()
    digits = re.sub(r"[^\d]", "", number)
    return ("+" + digits) if number.startswith("+") else digits


def link_member_sms(
    number: str, member_slug: str, *, linked_by: str | None = None, is_primary: bool = False
) -> None:
    normalized = _normalize_phone(number)
    if len(re.sub(r"\D", "", normalized)) < 7:
        raise ValueError("phone number must have at least 7 digits")
    _link_identity("sms", normalized, member_slug, is_primary=is_primary, linked_by=linked_by)


def sms_for_member(member_slug: str) -> list[str]:
    """All of a member's phone numbers, primary first."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT mi.identifier AS number FROM member_identities mi "
            "JOIN club_members m ON m.id = mi.member_id "
            "WHERE mi.surface = 'sms' AND m.slug = ? ORDER BY mi.is_primary DESC, mi.identifier",
            (member_slug,),
        ).fetchall()
    return [r["number"] for r in rows]


def list_member_sms() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT mi.identifier AS number, m.slug AS member_slug, mi.is_primary, "
            "mi.linked_by, mi.created_at, mi.updated_at "
            "FROM member_identities mi JOIN club_members m ON m.id = mi.member_id "
            "WHERE mi.surface = 'sms' ORDER BY m.slug, mi.identifier"
        ).fetchall()
    return [dict(r) for r in rows]


def _normalize_url(url: str) -> str:
    """Light normal form for a website URL: trim, default to https:// when no scheme is given, drop a
    trailing slash. Shape validation (a host with a dot) happens in link_member_website."""
    url = url.strip()
    if url and "://" not in url:
        url = "https://" + url
    return url.rstrip("/")


def _require_web_url(url: str) -> str:
    """Normalize + validate a website URL, or raise ValueError. Restricts to http/https: the URL
    renders as an <a href> on the PUBLIC member page, so a `javascript:`/`data:` scheme here would
    be stored XSS. A scheme check (not a substring of "://") is the gate: `javascript://x.y//comment`
    carries "://" and a dotted host but must be rejected."""
    url = _normalize_url(url)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or "." not in parts.netloc:
        raise ValueError("website must be an http(s) URL, e.g. https://example.com")
    return url


def link_member_website(
    url: str,
    member_slug: str,
    *,
    linked_by: str | None = None,
    is_primary: bool = False,
    label: str | None = None,
) -> None:
    url = _require_web_url(url)
    _link_identity(
        "website",
        url,
        member_slug,
        is_primary=is_primary,
        linked_by=linked_by,
        label=label,
    )


def update_member_website(
    old_url: str, member_slug: str, *, url: str | None = None, label: str | None = None
) -> bool:
    """Edit one of a member's existing websites in place: rename it (set/clear the display `label`)
    and/or change its URL. Unlike `link_member_website`'s upsert (which COALESCEs the label and can
    only add), this UPDATEs the row, so the name can be cleared and the URL changed without losing
    the row's primary flag. Returns True if a row was changed (False if the old URL wasn't found).
    Raises ValueError on a bad new URL or a collision with another of the member's websites."""
    old = _normalize_url(old_url)
    new = _require_web_url(url) if url else old
    label = (label or "").strip() or None
    with db.connect() as conn:
        mid = _member_id_for_slug(conn, member_slug)
        if mid is None:
            return False
        try:
            cur = conn.execute(
                "UPDATE member_identities SET identifier = ?, label = ?, updated_at = ? "
                "WHERE surface = 'website' AND member_id = ? AND identifier = ?",
                (new, label, _now(), mid, old),
            )
        except sqlite3.IntegrityError:
            raise ValueError("you already have that website") from None
        return cur.rowcount > 0


def websites_for_member(member_slug: str) -> list[str]:
    """All of a member's website URLs, primary first. Public — shown on the member's profile page."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT mi.identifier AS url FROM member_identities mi "
            "JOIN club_members m ON m.id = mi.member_id "
            "WHERE mi.surface = 'website' AND m.slug = ? "
            "ORDER BY mi.is_primary DESC, mi.created_at, mi.identifier",
            (member_slug,),
        ).fetchall()
    return [r["url"] for r in rows]


def list_member_websites() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT mi.identifier AS url, m.slug AS member_slug, mi.is_primary, "
            "mi.linked_by, mi.created_at, mi.updated_at "
            "FROM member_identities mi JOIN club_members m ON m.id = mi.member_id "
            "WHERE mi.surface = 'website' ORDER BY m.slug, mi.identifier"
        ).fetchall()
    return [dict(r) for r in rows]


# The first (and only) identity-removal path. Email is deliberately NOT removable: addresses anchor
# mailing-list attribution (mail_messages.member_id resolves through them), so dropping one would
# silently break who past + future list mail is attributed to.
def unlink_member_identity(surface: str, identifier: str, member_slug: str) -> bool:
    """Delete one of a member's own identities (member-scoped). Returns True if a row was removed.
    Refuses surface='email' — those can never be removed."""
    if surface == "email":
        raise ValueError("email addresses can't be removed — they anchor mailing-list attribution")
    with db.connect() as conn:
        mid = _member_id_for_slug(conn, member_slug)
        if mid is None:
            return False
        cur = conn.execute(
            "DELETE FROM member_identities WHERE surface = ? AND identifier = ? AND member_id = ?",
            (surface, identifier, mid),
        )
        return cur.rowcount > 0


def remove_member_website(url: str, member_slug: str) -> bool:
    return unlink_member_identity("website", _normalize_url(url), member_slug)


def remove_member_sms(number: str, member_slug: str) -> bool:
    return unlink_member_identity("sms", _normalize_phone(number), member_slug)
