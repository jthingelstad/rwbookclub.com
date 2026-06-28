"""Jinja2 rendering for the web app. Templates live in templates/; autoescape is on.

`render(template, request, **ctx)` injects the common context every page needs (the member's name,
admin flag, and CSRF token from the session) so route handlers only pass page-specific data.
"""

from __future__ import annotations

from pathlib import Path

import jinja2
from aiohttp import web

_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=True,
    trim_blocks=True,
    lstrip_blocks=True,
)


def render(template: str, request: web.Request, *, status: int = 200, **ctx) -> web.Response:
    session = request.get("session") or {}
    ctx.setdefault("member_name", session.get("name"))
    ctx.setdefault("member_slug", session.get("slug"))
    ctx.setdefault("is_admin", bool(session.get("a")))
    ctx.setdefault("csrf", session.get("csrf", ""))
    body = _env.get_template(template).render(**ctx)
    return web.Response(text=body, content_type="text/html", status=status)
