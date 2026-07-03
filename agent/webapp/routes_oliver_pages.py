"""Oliver's-brain pages — events, Book Cloud, memories — for both audiences, plus the review
markdown preview.

Split from routes_admin (which keeps the club-record editors: books/meetings/members/lists).
Admin gets the full grooming surface (edit/retire memories, delete events). Members get the
transparency views Jamie promised the club: the FULL Book Cloud (who + why — his call), and
"what Oliver knows about me" with a Retire button — a member can always see and correct what
the agent carries about them. Identity comes from the session, never from params.
"""

from __future__ import annotations

import asyncio

from aiohttp import web

from agent import clubdb, db
from agent import corpus_read as cr
from agent.mail.email_render import _render_markdown
from agent.webapp.render import render


def _form(request: web.Request):
    return request.get("form") or {}


def _members() -> list[dict]:
    with db.connect() as conn:
        return [{"slug": m["slug"], "name": m["name"], "id": m["id"], "current": bool(m["is_current"])}
                for m in clubdb.all_members(conn)]


# ── Club events (admin, read-only + delete) ──────────────────────────────────
def _event_categories() -> list[str]:
    with db.connect() as conn:
        return [r["category"] for r in conn.execute(
            "SELECT DISTINCT category FROM events ORDER BY category")]


def _load_events(category: str, member_slug: str, since: str, until: str, limit: int) -> list[dict]:
    member_id = None
    if member_slug:
        with db.connect() as conn:
            member_id = clubdb.member_id_for_slug(conn, member_slug)
    return db.timeline(category=category or None, member_id=member_id,
                       since=since or None, until=until or None, limit=limit)


async def events_page(request: web.Request) -> web.Response:
    q = request.query
    category, member = q.get("category", ""), q.get("member", "")
    since, until = q.get("since", ""), q.get("until", "")
    try:
        limit = min(max(int(q.get("limit") or 200), 1), 1000)
    except ValueError:
        limit = 200
    events = await asyncio.to_thread(_load_events, category, member, since, until, limit)
    categories = await asyncio.to_thread(_event_categories)
    members = sorted(await asyncio.to_thread(_members), key=lambda m: m["name"].lower())
    return render("admin_events.html", request, events=events, categories=categories,
                  members=members, query_string=request.query_string,
                  f={"category": category, "member": member,
                     "since": since, "until": until, "limit": limit})


async def event_delete(request: web.Request) -> web.Response:
    """POST /webapp/admin/events/delete — remove one timeline event by id (admin housekeeping).
    Redirects back to the events view, preserving the active filters via the `return` field."""
    form = _form(request)
    try:
        event_id = int(form.get("id", ""))
    except ValueError:
        raise web.HTTPFound("/webapp/admin/events")
    await asyncio.to_thread(db.delete_event, event_id)
    ret = (form.get("return") or "").strip()
    if not ret.startswith("/webapp/admin/events"):
        ret = "/webapp/admin/events"
    raise web.HTTPFound(ret)


# ── Book Cloud (admin + member — same full view, per Jamie) ──────────────────
def _bookcloud_view(*, view: str, q: str, member: str, kind: str, unread: bool,
                    limit: int) -> dict:
    """Filtered Book Cloud data. `view` is 'titles' (aggregated orbit — one row per title with
    first/last mention, who, count) or 'mentions' (raw rows, newest first). `unread` (titles
    view) drops titles matching books the club has read."""
    read_slugs = {b["slug"] for b in cr.books() if b.get("isRead")}
    if view == "mentions":
        rows = db.recent_book_cloud(limit=limit, query=q or None, member=member or None,
                                    kind=kind or None)
        return {"view": "mentions", "rows": rows}
    titles = db.book_cloud_titles(query=q or None, member=member or None, limit=limit)
    for t in titles:
        t["isRead"] = bool(t.get("book_slug") and t["book_slug"] in read_slugs)
    if unread:
        titles = [t for t in titles if not t["isRead"]]
    return {"view": "titles", "rows": titles}


def _bookcloud_kinds() -> list[str]:
    with db.connect() as conn:
        return [r["reason_kind"] for r in conn.execute(
            "SELECT DISTINCT reason_kind FROM book_cloud "
            "WHERE reason_kind IS NOT NULL ORDER BY reason_kind")]


async def _render_bookcloud(request: web.Request, template: str, action: str) -> web.Response:
    p = request.query
    view = "mentions" if p.get("view") == "mentions" else "titles"
    q, member, kind = p.get("q", "").strip(), p.get("member", ""), p.get("kind", "")
    unread = p.get("include_read") != "1"    # titles view defaults to unread-only
    try:
        limit = min(max(int(p.get("limit") or 200), 1), 500)
    except ValueError:
        limit = 200
    data = await asyncio.to_thread(_bookcloud_view, view=view, q=q, member=member,
                                   kind=kind, unread=unread, limit=limit)
    kinds = await asyncio.to_thread(_bookcloud_kinds)
    members = sorted(await asyncio.to_thread(_members), key=lambda m: m["name"].lower())
    return render(template, request, rows=data["rows"], kinds=kinds, members=members,
                  action=action,
                  f={"view": data["view"], "q": q, "member": member, "kind": kind,
                     "unread": unread, "limit": limit})


async def bookcloud_page(request: web.Request) -> web.Response:
    return await _render_bookcloud(request, "admin_bookcloud.html", "/webapp/admin/bookcloud")


async def member_bookcloud_page(request: web.Request) -> web.Response:
    return await _render_bookcloud(request, "bookcloud.html", "/webapp/bookcloud")


# ── Memories ─────────────────────────────────────────────────────────────────
# Admin: the full grooming surface (search all, edit, retire). Member: their own notes with
# Retire only — provenance stays clean (a wrong note is retired, not rewritten by its subject).
def _memory_sources() -> list[str]:
    with db.connect() as conn:
        return [r["source"] for r in conn.execute(
            "SELECT DISTINCT source FROM memories WHERE status = 'active' AND source IS NOT NULL "
            "ORDER BY source")]


async def memories_page(request: web.Request) -> web.Response:
    p = request.query
    q, subject, scope, source = (p.get("q", "").strip(), p.get("subject", "").strip(),
                                 p.get("scope", ""), p.get("source", ""))
    try:
        limit = min(max(int(p.get("limit") or 200), 1), 500)
    except ValueError:
        limit = 200
    rows = await asyncio.to_thread(
        db.get_memories, subject=subject or None, scope=scope or None,
        query=q or None, source=source or None, limit=limit)
    total = await asyncio.to_thread(db.count_memories)
    sources = await asyncio.to_thread(_memory_sources)
    members = sorted(await asyncio.to_thread(_members), key=lambda m: m["name"].lower())
    return render("admin_memories.html", request, rows=rows, total=total, sources=sources,
                  members=members,
                  f={"q": q, "subject": subject, "scope": scope, "source": source, "limit": limit})


async def memory_action(request: web.Request) -> web.Response:
    form = _form(request)
    op = form.get("op", "")
    try:
        memory_id = int(form.get("id") or 0)
    except ValueError:
        memory_id = 0
    if op == "edit" and memory_id and (form.get("note") or "").strip():
        await asyncio.to_thread(db.update_memory, memory_id, form["note"].strip())
    elif op == "retire" and memory_id:
        await asyncio.to_thread(db.delete_memory, memory_id)
    # Memories are private (never rendered to the public site) — no mark_dirty.
    ret = (form.get("return") or "").strip()
    if not ret.startswith("/webapp/admin/memories"):
        ret = "/webapp/admin/memories"
    raise web.HTTPFound(ret)


async def member_memories_page(request: web.Request) -> web.Response:
    slug = request["session"]["slug"]
    rows = await asyncio.to_thread(db.get_memories, subject=slug, scope="member", limit=500)
    return render("memories.html", request, rows=rows)


async def member_memory_retire(request: web.Request) -> web.Response:
    """POST /webapp/memories/act — a member retires one of THEIR OWN memories. The id must
    belong to a memory whose subject is the session's member (checked server-side; the id in
    the form is never trusted alone)."""
    slug = request["session"]["slug"]
    form = _form(request)
    try:
        memory_id = int(form.get("id") or 0)
    except ValueError:
        memory_id = 0
    own = await asyncio.to_thread(db.get_memories, subject=slug, scope="member", limit=500)
    if memory_id and any(r["id"] == memory_id for r in own):
        await asyncio.to_thread(db.delete_memory, memory_id)
    raise web.HTTPFound("/webapp/memories")


# ── Review markdown preview ───────────────────────────────────────────────────
async def preview(request: web.Request) -> web.Response:
    """POST /webapp/preview — render markdown to HTML for the review composer's Preview toggle.
    Reuses the email renderer, whose _EscapeRawHtml extension neutralizes raw HTML (no live
    tags), so the returned fragment is inert; CSP (script-src 'self') is the backstop."""
    text = (_form(request).get("text") or "")[:20000]
    html = await asyncio.to_thread(_render_markdown, text)
    return web.json_response({"html": html})


def add_admin_routes(app: web.Application) -> None:
    app.add_routes([
        web.get("/webapp/admin/events", events_page),
        web.post("/webapp/admin/events/delete", event_delete),
        web.get("/webapp/admin/bookcloud", bookcloud_page),
        web.get("/webapp/admin/memories", memories_page),
        web.post("/webapp/admin/memories/act", memory_action),
    ])


def add_member_routes(app: web.Application) -> None:
    app.add_routes([
        web.get("/webapp/memories", member_memories_page),
        web.post("/webapp/memories/act", member_memory_retire),
        web.get("/webapp/bookcloud", member_bookcloud_page),
        web.post("/webapp/preview", preview),
    ])
