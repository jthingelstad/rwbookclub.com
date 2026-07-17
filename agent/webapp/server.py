"""The member web app server: an aiohttp app run inside the bot process, reached over Tailscale
Funnel. Starts on demand (`/oliver my-club`), shuts down after idle. One middleware handles activity
tracking, session auth, the admin gate, and CSRF. Public-data writes mark the site dirty; it's
rebuilt only on an explicit Publish or on idle shutdown (deferred-publish model).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiohttp import web

from agent import config, publishing
from agent.webapp import routes_admin, routes_member, routes_oliver_pages, sessions, state
from agent.webapp.render import render

log = logging.getLogger("oliver.webapp")

_IDLE_TIMEOUT = timedelta(minutes=15)
_CHECK_INTERVAL = 60  # seconds between idle checks

_lock = asyncio.Lock()
_runner: web.AppRunner | None = None
_site: web.TCPSite | None = None
_idle_task: asyncio.Task | None = None
_last_activity: datetime | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_ajax(request: web.Request) -> bool:
    return request.headers.get("X-Requested-With") == "fetch"


# Chat clients fetch a shared URL to build a link preview. They must NOT spend the single-use
# token — only the human's tap should. Match the common unfurlers' user agents.
_PREVIEW_BOTS = (
    "discordbot",
    "facebookexternalhit",
    "slackbot",
    "telegrambot",
    "whatsapp",
    "applebot",
    "twitterbot",
    "linkedinbot",
    "embedly",
    "preview",
    "bot/",
)


def _is_link_preview(request: web.Request) -> bool:
    ua = request.headers.get("User-Agent", "").lower()
    return any(b in ua for b in _PREVIEW_BOTS)


# Sent on every response. The app is public (Funnel): nosniff blocks MIME-confusion, DENY/none stops
# clickjacking of the admin editor, no-referrer keeps the one-time `?t=` token out of Referer, and a
# self-only CSP limits what an injected page could load (inline scripts are still allowed — the
# templates rely on them — so this is a backstop, not the primary XSS control, which is autoescape).
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # No 'unsafe-inline' anywhere: all CSS/JS is served from /webapp/static/, so this CSP is a
    # real XSS control on the public Funnel endpoint, not a documented backstop.
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; "
    "style-src 'self'; script-src 'self'; "
    "frame-ancestors 'none'",
}


@web.middleware
async def _security_headers(request: web.Request, handler):
    try:
        resp = await handler(request)
    except web.HTTPException as exc:
        for k, v in _SECURITY_HEADERS.items():
            exc.headers.setdefault(k, v)
        raise
    for k, v in _SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    return resp


# ── Middleware: activity + auth + admin gate + CSRF ──────────────────────────
@web.middleware
async def _mw(request: web.Request, handler):
    global _last_activity
    _last_activity = _now()
    path = request.path

    if path == "/healthz" or path.startswith("/webapp/static/"):
        return await handler(request)
    # Token-exchange entry is the one authenticated-by-token (not cookie) route.
    if path == "/webapp" and request.query.get("t"):
        return await handler(request)

    session = sessions.read_session(request.cookies.get(sessions.COOKIE_NAME))
    if session is None:
        if _is_ajax(request):
            return web.json_response({"error": "session expired"}, status=401)
        return render("expired.html", request, status=401)
    request["session"] = session

    if path.startswith("/webapp/admin") and not session.get("a"):
        return web.Response(status=403, text="Admins only.")

    if request.method == "POST":
        form = await request.post()
        request["form"] = form
        if not sessions.csrf_ok(session, form.get("csrf")):
            if _is_ajax(request):
                return web.json_response({"error": "bad csrf"}, status=403)
            return web.Response(status=403, text="Bad CSRF token — reload and try again.")

    resp = await handler(request)
    # Sliding renewal: an ACTIVE visit never expires mid-edit (abandoned ones still die on TTL).
    fresh = sessions.refresh_if_stale(session)
    if fresh:
        try:
            resp.set_cookie(
                sessions.COOKIE_NAME,
                fresh,
                httponly=True,
                secure=True,
                samesite="Lax",
                max_age=int(sessions._SESSION_TTL.total_seconds()),
            )
        except AttributeError:
            pass  # streamed/exception responses without set_cookie — renew on the next request
    return resp


# ── Core routes (entry, home, publish, health) ───────────────────────────────
async def healthz(request: web.Request) -> web.Response:
    return web.Response(text="ok\n")


async def entry(request: web.Request) -> web.Response:
    """GET /webapp — token exchange (?t=) → session cookie + redirect; otherwise the home dashboard."""
    token = request.query.get("t")
    if token:
        if _is_link_preview(request):
            # A chat client unfurling the link — answer without spending the single-use token.
            return web.Response(text="Open this link to manage your R/W Book Club data.")
        member = await asyncio.to_thread(sessions.consume_token, token)
        if member is None:
            return render("expired.html", request, status=401)
        resp = web.HTTPFound("/webapp")
        resp.set_cookie(
            sessions.COOKIE_NAME,
            sessions.make_session(member),
            httponly=True,
            secure=True,
            samesite="Lax",
            max_age=int(sessions._SESSION_TTL.total_seconds()),
        )
        raise resp
    session = request.get("session") or {}
    ctx = await asyncio.to_thread(routes_member.home_context, session.get("slug", ""))
    return render("home.html", request, **ctx)


async def publish_now(request: web.Request) -> web.Response:
    """POST /webapp/publish — push the accumulated session's changes to the live site."""
    had = state.is_dirty()
    state.clear_dirty()
    if had:
        _trigger_publish()
    return web.json_response({"ok": True, "published": had})


def _trigger_publish() -> None:
    publishing.schedule()


# ── Lifecycle (on-demand start, idle shutdown, publish-if-dirty) ─────────────
def _build_app() -> web.Application:
    # client_max_size caps request bodies (the app only takes small forms) so the shared bot process
    # can't be memory-pressured by a giant POST. Outer middleware runs first: headers wrap everything.
    app = web.Application(middlewares=[_security_headers, _mw], client_max_size=256 * 1024)
    app.add_routes(
        [
            web.get("/healthz", healthz),
            web.get("/webapp", entry),
            web.post("/webapp/publish", publish_now),
        ]
    )
    from pathlib import Path

    app.router.add_static("/webapp/static/", Path(__file__).parent / "static")
    routes_member.add_routes(app)
    routes_admin.add_routes(app)
    routes_oliver_pages.add_member_routes(app)
    routes_oliver_pages.add_admin_routes(app)
    return app


async def ensure_running() -> None:
    """Start the loopback server if it isn't already, and (re)arm the idle clock. Idempotent."""
    global _runner, _site, _idle_task, _last_activity
    async with _lock:
        _last_activity = _now()
        if _runner is not None:
            return
        # Fail closed: never serve the public (Funnel) app signed with the known dev literal, or a
        # missing key — that would let anyone forge an admin session cookie. Require a real secret.
        if not config.WEBAPP_SECRET or config.WEBAPP_SECRET == config.WEBAPP_DEV_SECRET:
            raise RuntimeError(
                "Refusing to start the web app: set WEBAPP_SECRET to a dedicated real "
                "secret — signing sessions with the dev default would allow forged admin cookies."
            )
        runner = web.AppRunner(_build_app())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", config.WEBAPP_PORT)
        await site.start()
        _runner, _site = runner, site
        _idle_task = asyncio.create_task(_idle_watcher())
        log.info(
            "webapp started on 127.0.0.1:%d (public: %s); idle shutoff after %d min",
            config.WEBAPP_PORT,
            config.WEBAPP_BASE_URL,
            _IDLE_TIMEOUT.total_seconds() // 60,
        )


async def _idle_watcher() -> None:
    while True:
        await asyncio.sleep(_CHECK_INTERVAL)
        async with _lock:
            if _runner is None:
                return
            if _last_activity is None or _now() - _last_activity >= _IDLE_TIMEOUT:
                had = state.is_dirty()
                await _do_stop()
                if had:
                    state.clear_dirty()
                    _trigger_publish()  # session's accumulated changes go live
                return


async def _do_stop() -> None:
    """Tear down the server. Caller must hold _lock."""
    global _runner, _site
    if _site is not None:
        await _site.stop()
    if _runner is not None:
        await _runner.cleanup()
    _runner = _site = None
    log.info("webapp stopped (idle); no listener until the next /oliver my-club")
