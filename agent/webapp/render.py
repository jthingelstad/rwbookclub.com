"""Jinja2 rendering for the web app. Templates live in templates/; autoescape is on.

`render(template, request, **ctx)` injects the common context every page needs (the member's name,
admin flag, and CSRF token from the session) so route handlers only pass page-specific data.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import jinja2
from aiohttp import web

from agent.webapp import state

_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=True,
    trim_blocks=True,
    lstrip_blocks=True,
)

_STATIC = Path(__file__).parent / "static"


def _assets_version() -> str:
    """A short content hash over the static assets — cache-busts browsers on deploy."""
    h = hashlib.sha256()
    for f in sorted(_STATIC.glob("*")):
        h.update(f.name.encode())
        h.update(str(f.stat().st_mtime_ns).encode())
    return h.hexdigest()[:10]


_ASSETS_V = _assets_version()


def render(template: str, request: web.Request, *, status: int = 200, **ctx) -> web.Response:
    session = request.get("session") or {}
    ctx.setdefault("member_name", session.get("name"))
    ctx.setdefault("member_slug", session.get("slug"))
    ctx.setdefault("is_admin", bool(session.get("a")))
    ctx.setdefault("csrf", session.get("csrf", ""))
    ctx.setdefault("site_dirty", state.is_dirty())  # the Publish button's honesty dot
    ctx.setdefault("assets_v", _ASSETS_V)
    body = _env.get_template(template).render(**ctx)
    return web.Response(text=body, content_type="text/html", status=status)
