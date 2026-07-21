"""Oliver-private structured member preferences.

Pronouns are keyed to the canonical member id but are not part of ``club_members`` and are never
projected into the corpus or website. They enter only Oliver's private system context as a silent
grammar aid. The small character allowlist also prevents a self-service profile value from becoming
prompt instructions when that context is assembled.
"""

from __future__ import annotations

from datetime import datetime, timezone

from agent import db

MAX_PRONOUNS_LENGTH = 64


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_pronouns(value: str | None) -> str | None:
    """Normalize a display value, with blank meaning "unknown / remove"."""
    raw = value or ""
    if any(char.isspace() and char != " " for char in raw):
        raise ValueError("pronouns must be entered on one line")
    stripped = raw.strip()
    if not stripped:
        return None
    parts = [part.strip() for part in stripped.split("/")]
    normalized = "/".join(parts)
    if not normalized:
        return None
    if len(normalized) > MAX_PRONOUNS_LENGTH:
        raise ValueError(f"pronouns must be {MAX_PRONOUNS_LENGTH} characters or fewer")
    if not 2 <= len(parts) <= 4 or any(
        not part or any(not token.isalpha() for token in part.split("-")) for part in parts
    ):
        raise ValueError("pronouns must be 2-4 slash-separated words, such as he/him")
    return normalized


def for_member(member_slug: str) -> str | None:
    """Return one member's private pronouns, or None when they have not supplied a value."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT p.pronouns FROM member_preferences p "
            "JOIN club_members m ON m.id = p.member_id WHERE m.slug = ?",
            (member_slug,),
        ).fetchone()
    return row["pronouns"] if row else None


def for_current_members() -> list[dict]:
    """Explicit pronouns for current members, including Oliver, for private prompt context."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT m.slug, m.name, p.pronouns FROM club_members m "
            "JOIN member_preferences p ON p.member_id = m.id "
            "WHERE m.is_current = 1 ORDER BY m.name COLLATE NOCASE"
        ).fetchall()
    return [dict(row) for row in rows]


def set_for_member(member_slug: str, value: str | None, *, source: str) -> str | None:
    """Set or clear a private value. Returns the normalized stored value.

    The slug is resolved only at this boundary; persistence uses the canonical integer member id.
    """
    pronouns = normalize_pronouns(value)
    source = " ".join((source or "").split())
    if not source:
        raise ValueError("pronoun source is required")
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM club_members WHERE slug = ?", (member_slug,)).fetchone()
        if row is None:
            raise ValueError(f"no club member with slug {member_slug!r}")
        if pronouns is None:
            conn.execute("DELETE FROM member_preferences WHERE member_id = ?", (row["id"],))
            return None
        conn.execute(
            "INSERT INTO member_preferences (member_id, pronouns, source, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(member_id) DO UPDATE SET "
            "pronouns=excluded.pronouns, source=excluded.source, "
            "updated_at=excluded.updated_at",
            (row["id"], pronouns, source, _now()),
        )
    return pronouns
