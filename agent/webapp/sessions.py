"""Auth for the member web app: one-time link tokens + HMAC-signed session cookies + CSRF.

The Discord `/oliver my-club` command mints a single-use token (the Discord identity link IS the auth).
On first load the server *consumes* it and issues a signed session cookie carrying the member's id,
admin flag, and a per-session CSRF secret. Every later request derives identity from that cookie —
never from request params — so a member can only ever see and edit their own data.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone

from agent import config, db

COOKIE_NAME = "oliver_session"
_TOKEN_TTL = timedelta(minutes=15)
_SESSION_TTL = timedelta(minutes=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _secret() -> bytes:
    return config.WEBAPP_SECRET.encode()


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ── One-time link tokens ─────────────────────────────────────────────────────
def mint_token(member_id: int, *, is_admin: bool, ttl: timedelta = _TOKEN_TTL) -> str:
    """Create a single-use web-app link token for a member; returns the opaque token string."""
    token = secrets.token_urlsafe(32)
    now = _now()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO webapp_tokens (token, member_id, is_admin, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (token, member_id, 1 if is_admin else 0, now.isoformat(), (now + ttl).isoformat()),
        )
    return token


def _row_to_member(row) -> dict:
    return {
        "member_id": row["member_id"],
        "slug": row["slug"],
        "name": row["name"],
        "is_admin": bool(row["is_admin"]),
    }


def _lookup(conn, token: str):
    return conn.execute(
        "SELECT t.member_id, t.is_admin, t.expires_at, t.used_at, m.slug, m.name "
        "FROM webapp_tokens t JOIN club_members m ON m.id = t.member_id WHERE t.token = ?",
        (token,),
    ).fetchone()


def _valid(row) -> bool:
    if row is None or row["used_at"]:
        return False
    try:
        return datetime.fromisoformat(row["expires_at"]) >= _now()
    except ValueError, TypeError:
        return False


def resolve_token(token: str | None) -> dict | None:
    """Non-consuming check: return the member dict if the token exists, is unused, and unexpired."""
    if not token:
        return None
    with db.connect() as conn:
        row = _lookup(conn, token)
    return _row_to_member(row) if _valid(row) else None


def consume_token(token: str | None) -> dict | None:
    """Single-use exchange: validate and atomically mark the token used. Returns the member or None."""
    if not token:
        return None
    with db.connect() as conn:
        row = _lookup(conn, token)
        if not _valid(row):
            return None
        # Atomic single-use gate: only the request that flips used_at NULL→now wins. Two concurrent
        # exchanges of the same token both pass _valid (a TOCTOU window), so the conditional UPDATE —
        # not the SELECT — is the authority. rowcount 0 means another request already consumed it.
        cur = conn.execute(
            "UPDATE webapp_tokens SET used_at = ? WHERE token = ? AND used_at IS NULL",
            (_now().isoformat(), token),
        )
        if cur.rowcount == 0:
            return None
        return _row_to_member(row)


# ── Signed session cookies ───────────────────────────────────────────────────
def make_session(member: dict) -> str:
    """Serialize + sign a session for a resolved member. Includes a per-session CSRF secret."""
    payload = {
        "m": member["member_id"],
        "a": 1 if member["is_admin"] else 0,
        "slug": member["slug"],
        "name": member["name"],
        "csrf": secrets.token_urlsafe(16),
        "exp": (_now() + _SESSION_TTL).isoformat(),
    }
    raw = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64e(hmac.new(_secret(), raw.encode(), hashlib.sha256).digest())
    return f"{raw}.{sig}"


def refresh_if_stale(session: dict) -> str | None:
    """A re-signed cookie with a fresh expiry when the session is past half its TTL, else None.

    Sliding renewal WITHIN an active visit: someone composing for 40 minutes never gets cut off
    mid-save, while an abandoned session still dies _SESSION_TTL after its last request. The
    payload (identity, admin flag, CSRF secret) is unchanged — only `exp` moves."""
    try:
        exp = datetime.fromisoformat(session["exp"])
    except KeyError, ValueError, TypeError:
        return None
    if exp - _now() > _SESSION_TTL / 2:
        return None
    payload = {**session, "exp": (_now() + _SESSION_TTL).isoformat()}
    raw = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64e(hmac.new(_secret(), raw.encode(), hashlib.sha256).digest())
    return f"{raw}.{sig}"


def read_session(cookie: str | None) -> dict | None:
    """Verify a session cookie's signature + expiry; return the payload dict or None."""
    if not cookie or "." not in cookie:
        return None
    raw, _, sig = cookie.partition(".")
    expected = _b64e(hmac.new(_secret(), raw.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_b64d(raw))
        exp = datetime.fromisoformat(payload["exp"])
    except ValueError, TypeError, KeyError:
        return None
    return None if exp < _now() else payload


def csrf_ok(session: dict, supplied: str | None) -> bool:
    return bool(supplied) and hmac.compare_digest(session.get("csrf", ""), supplied)
