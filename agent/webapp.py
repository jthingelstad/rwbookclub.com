"""Member web app — a tiny aiohttp server run inside the bot process, reached over Tailscale Funnel.

Connectivity spike. The `/oliver webapp` Discord command mints a one-time token (the Discord identity
IS the auth) and hands the member a URL; this server resolves the token to a member and proves the
round trip: Discord → browser (via Funnel) → local server → authoritative DB → back. The eventual
per-member CRUD editor builds on this scaffold (token table, in-process server) but tightens the
token model to single-use + signed session cookie.

aiohttp ships with discord.py, so this adds no dependency. The server binds loopback only; Tailscale
Funnel maps the public 443 → this port.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

from aiohttp import web

from agent import config, db

log = logging.getLogger("oliver.webapp")

_TOKEN_TTL = timedelta(minutes=15)
_runner: web.AppRunner | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Tokens ───────────────────────────────────────────────────────────────────
def mint_token(member_id: int, *, is_admin: bool, ttl: timedelta = _TOKEN_TTL) -> str:
    """Create a web-app link token for a member and return the opaque token string."""
    token = secrets.token_urlsafe(32)
    now = _now()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO webapp_tokens (token, member_id, is_admin, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (token, member_id, 1 if is_admin else 0, now.isoformat(), (now + ttl).isoformat()),
        )
    return token


def resolve_token(token: str | None) -> dict | None:
    """Resolve a token to {member_id, slug, name, is_admin} if it exists and hasn't expired, else None.

    Spike behavior: tokens are reusable until expiry (so the page GET and the ping POST share one).
    `used_at` is reserved for the production single-use exchange.
    """
    if not token:
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT t.member_id, t.is_admin, t.expires_at, m.slug, m.name "
            "FROM webapp_tokens t JOIN club_members m ON m.id = t.member_id "
            "WHERE t.token = ?",
            (token,),
        ).fetchone()
    if row is None:
        return None
    try:
        expires = datetime.fromisoformat(row["expires_at"])
    except (ValueError, TypeError):
        return None
    if expires < _now():
        return None
    return {
        "member_id": row["member_id"],
        "slug": row["slug"],
        "name": row["name"],
        "is_admin": bool(row["is_admin"]),
    }


# ── Rendering ────────────────────────────────────────────────────────────────
def _render_page(member: dict, token: str) -> str:
    name = html.escape(member["name"] or member["slug"])
    slug = html.escape(member["slug"])
    admin = " (admin)" if member["is_admin"] else ""
    token_js = json.dumps(token)  # safe JS string literal
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>R/W Book Club — Oliver</title>
<style>
  body {{ font: 16px/1.5 -apple-system, system-ui, sans-serif; max-width: 32rem;
         margin: 3rem auto; padding: 0 1.25rem; color: #1a1a1a; }}
  .ok {{ color: #1a7f37; font-weight: 600; font-size: 1.2rem; }}
  .muted {{ color: #666; font-size: .9rem; }}
  button {{ font: inherit; padding: .6rem 1rem; border: 1px solid #888;
           border-radius: 6px; background: #f6f6f6; cursor: pointer; }}
  code {{ background: #f0f0f0; padding: .1rem .3rem; border-radius: 3px; }}
  #out {{ margin-top: 1rem; font-weight: 600; }}
</style></head>
<body>
  <p class="ok">✅ Round trip works.</p>
  <p>Hello, <strong>{name}</strong> — you're <code>{slug}</code>{admin}.</p>
  <p class="muted">This page reached Oliver's local database on the Mac through Tailscale Funnel,
  authenticated by your Discord link. No password, no cloud database.</p>
  <button id="ping">Ping Oliver (write test)</button>
  <div id="out"></div>
  <script>
    document.getElementById('ping').addEventListener('click', async () => {{
      const out = document.getElementById('out');
      out.textContent = 'Pinging…';
      try {{
        const r = await fetch('/webapp/ping', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
          body: 't=' + encodeURIComponent({token_js})
        }});
        const j = await r.json();
        out.textContent = r.ok ? ('✅ Write reached the database at ' + j.at)
                               : ('⚠️ ' + (j.error || r.status));
      }} catch (e) {{ out.textContent = '⚠️ ' + e; }}
    }});
  </script>
</body></html>"""


# ── Routes ───────────────────────────────────────────────────────────────────
async def healthz(request: web.Request) -> web.Response:
    return web.Response(text="ok\n")


async def webapp_page(request: web.Request) -> web.Response:
    member = resolve_token(request.query.get("t"))
    if member is None:
        return web.Response(
            status=401,
            text="This link is invalid or has expired. Run /oliver webapp in Discord for a new one.\n",
        )
    return web.Response(text=_render_page(member, request.query["t"]), content_type="text/html")


async def webapp_ping(request: web.Request) -> web.Response:
    data = await request.post()
    member = resolve_token(data.get("t"))
    if member is None:
        return web.json_response({"error": "invalid or expired link"}, status=401)
    at = _now().isoformat(timespec="seconds")
    await asyncio.to_thread(
        db.add_activity, "webapp_ping", "Web app round-trip ping",
        f"member: {member['slug']}\nproof that a browser write reached the local DB via Funnel",
    )
    return web.json_response({"ok": True, "at": at})


# ── Lifecycle ────────────────────────────────────────────────────────────────
async def start_webapp() -> None:
    """Start the loopback aiohttp server on the bot's event loop (idempotent)."""
    global _runner
    if _runner is not None:
        return
    app = web.Application()
    app.add_routes([
        web.get("/healthz", healthz),
        web.get("/webapp", webapp_page),
        web.post("/webapp/ping", webapp_ping),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", config.WEBAPP_PORT)
    await site.start()
    _runner = runner  # module-global keeps the runner alive (else GC'd)
    log.info("webapp listening on 127.0.0.1:%d (public: %s)", config.WEBAPP_PORT, config.WEBAPP_BASE_URL)
