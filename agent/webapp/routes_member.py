"""Member-facing routes: bulk ratings grid, review composer, lists, profile/contact.

Every handler derives the member from the signed session (`request["session"]`), never from params,
and reuses the existing writers (`clubdb`, `reviews`, `lists`, `db.link_member_*`). Public-data writes
call `state.mark_dirty()`; the site is published later (Publish button / idle shutdown).
"""

from __future__ import annotations

import asyncio

from aiohttp import web

from agent import clubdb, corpus_gen, db
from agent import corpus_read as cr
from agent.club import lists as lists_writer
from agent.club import reviews as reviews_writer
from agent.webapp import state
from agent.webapp.render import render
from corpus.paths import DATA_DIR


def _form(request: web.Request):
    return request.get("form") or {}


def _load_books() -> list[dict]:
    with db.connect() as conn:
        books = clubdb.all_books(conn)
    return sorted(({"slug": b["slug"], "title": b["title"]} for b in books),
                  key=lambda b: b["title"].lower())


def _member_ratings(member_slug: str) -> dict[str, dict]:
    with db.connect() as conn:
        return {r["book_slug"]: r for r in clubdb.all_reviews(conn)
                if r["member_slug"] == member_slug}


# ── Ratings — bulk grid ──────────────────────────────────────────────────────
async def ratings_page(request: web.Request) -> web.Response:
    slug = request["session"]["slug"]
    books = await asyncio.to_thread(_load_books)
    mine = await asyncio.to_thread(_member_ratings, slug)
    rows = [{
        "slug": b["slug"], "title": b["title"],
        "rating": (mine.get(b["slug"]) or {}).get("rating"),
        "dnf": bool((mine.get(b["slug"]) or {}).get("dnf")),
    } for b in books]
    return render("ratings.html", request, books=rows)


def _do_set_rating(book_slug: str, member_id: int, *, rating, dnf) -> bool:
    with db.connect() as conn:
        book_id = clubdb.book_id_for_slug(conn, book_slug)
        if book_id is None:
            return False
        review_id = clubdb.set_rating(conn, book_id, member_id, rating=rating, dnf=dnf)
        corpus_gen.write_review_file(conn, review_id, DATA_DIR)
    return True


async def ratings_set(request: web.Request) -> web.Response:
    form = _form(request)
    session = request["session"]
    book_slug = (form.get("book_slug") or "").strip()
    dnf = form.get("dnf") in ("1", "true", "on")
    rating = None
    if not dnf and form.get("rating"):
        try:
            rating = int(form["rating"])
        except ValueError:
            return web.json_response({"error": "bad rating"}, status=400)
        if not 1 <= rating <= 5:
            return web.json_response({"error": "rating must be 1-5"}, status=400)
    ok = await asyncio.to_thread(_do_set_rating, book_slug, session["m"], rating=rating, dnf=dnf)
    if not ok:
        return web.json_response({"error": "unknown book"}, status=404)
    state.mark_dirty()
    return web.json_response({"ok": True, "rating": rating, "dnf": dnf})


# ── Review — composer ────────────────────────────────────────────────────────
async def reviews_page(request: web.Request) -> web.Response:
    slug = request["session"]["slug"]
    books = await asyncio.to_thread(_load_books)
    mine = await asyncio.to_thread(_member_ratings, slug)
    reviewed = {s for s, r in mine.items() if (r.get("body") or r.get("rating") or r.get("dnf"))}
    return render("reviews_index.html", request, books=books, reviewed=reviewed)


async def review_compose(request: web.Request) -> web.Response:
    slug = request["session"]["slug"]
    book_slug = request.match_info["book"]
    book = await asyncio.to_thread(cr.find_book, book_slug)
    if not book:
        return web.Response(status=404, text="No such book.")
    existing = await asyncio.to_thread(
        lambda: next((r for r in cr._reviews_for(book_slug=book["slug"], member_slug=slug)), None))
    return render("review.html", request, book=book, existing=existing or {})


async def review_submit(request: web.Request) -> web.Response:
    form = _form(request)
    slug = request["session"]["slug"]
    book_slug = (form.get("book_slug") or "").strip()
    try:
        await asyncio.to_thread(
            reviews_writer.write_review, book_slug, slug,
            rating=form.get("rating"), review=form.get("body"),
            recommend=form.get("recommend"), discussion=form.get("discussion"),
            quote=form.get("quote"),
        )
    except reviews_writer.ReviewError as e:
        book = await asyncio.to_thread(cr.find_book, book_slug)
        return render("review.html", request, status=400, book=book or {"slug": book_slug, "title": book_slug},
                      existing=dict(form), error=str(e))
    state.mark_dirty()
    raise web.HTTPFound("/webapp/reviews")


# ── Lists ────────────────────────────────────────────────────────────────────
async def lists_page(request: web.Request) -> web.Response:
    slug = request["session"]["slug"]
    is_admin = bool(request["session"].get("a"))
    all_lists = await asyncio.to_thread(cr.lists)
    mine = [lst for lst in all_lists if lst.get("owner") == slug]
    club = [lst for lst in all_lists if lst.get("scope") == "club"] if is_admin else []
    books = await asyncio.to_thread(_load_books)
    titles = {b["slug"]: b["title"] for b in books}
    return render("lists.html", request, lists=mine, club_lists=club, books=books, titles=titles)


async def lists_create(request: web.Request) -> web.Response:
    form = _form(request)
    session = request["session"]
    scope = "club" if (form.get("scope") == "club" and session.get("a")) else "member"
    try:
        await asyncio.to_thread(
            lists_writer.create_list, form.get("name", ""), form.get("description"),
            owner_slug=None if scope == "club" else session["slug"], scope=scope)
    except lists_writer.ListError:
        pass  # fall through to re-render; the page shows current state
    state.mark_dirty()
    raise web.HTTPFound("/webapp/lists")


async def lists_action(request: web.Request) -> web.Response:
    """POST /webapp/lists/act — add-book / remove-book / edit / delete, dispatched by `op`."""
    form = _form(request)
    session = request["session"]
    actor, is_admin = session["slug"], bool(session.get("a"))
    op = form.get("op")
    ref = form.get("list", "")
    try:
        if op == "add-book":
            await asyncio.to_thread(lists_writer.add_book, ref, form.get("book", ""),
                                    form.get("note"), actor_slug=actor, is_admin=is_admin)
        elif op == "remove-book":
            await asyncio.to_thread(lists_writer.remove_book, ref, form.get("book", ""),
                                    actor_slug=actor, is_admin=is_admin)
        elif op == "edit":
            await asyncio.to_thread(lists_writer.edit_list, ref, name=form.get("name") or None,
                                    description=form.get("description"), actor_slug=actor, is_admin=is_admin)
        elif op == "delete":
            await asyncio.to_thread(lists_writer.delete_list, ref, actor_slug=actor, is_admin=is_admin)
        elif op in ("move-up", "move-down"):
            await asyncio.to_thread(lists_writer.move_book, ref, form.get("book", ""),
                                    up=(op == "move-up"), actor_slug=actor, is_admin=is_admin)
    except lists_writer.ListError:
        pass
    state.mark_dirty()
    raise web.HTTPFound("/webapp/lists")


# ── Profile / contact ────────────────────────────────────────────────────────
async def profile_page(request: web.Request) -> web.Response:
    slug = request["session"]["slug"]
    websites = await asyncio.to_thread(db.member_handles, slug, "website")
    emails = await asyncio.to_thread(db.member_handles, slug, "email")
    phones = await asyncio.to_thread(db.member_handles, slug, "sms")
    return render("profile.html", request, websites=websites, emails=emails, phones=phones)


async def profile_action(request: web.Request) -> web.Response:
    form = _form(request)
    slug = request["session"]["slug"]
    op = form.get("op")
    val = (form.get("value") or "").strip()
    published = False
    try:
        if op == "add-website" and val:
            await asyncio.to_thread(db.link_member_website, val, slug,
                                    linked_by="webapp", label=(form.get("label") or "").strip() or None)
            published = True
        elif op == "remove-website" and val:
            await asyncio.to_thread(db.remove_member_website, val, slug)
            published = True
        elif op == "add-email" and val:
            await asyncio.to_thread(db.link_member_email, val, slug, linked_by="webapp")
        elif op == "add-phone" and val:
            await asyncio.to_thread(db.link_member_sms, val, slug, linked_by="webapp")
        elif op == "remove-phone" and val:
            await asyncio.to_thread(db.remove_member_sms, val, slug)
        elif op == "primary-website" and val:
            await asyncio.to_thread(db.set_primary_identity, slug, "website", val)
            published = True  # website order is public
        elif op == "primary-email" and val:
            await asyncio.to_thread(db.set_primary_identity, slug, "email", val)
        elif op == "primary-phone" and val:
            await asyncio.to_thread(db.set_primary_identity, slug, "sms", val)
    except ValueError:
        pass  # bad input — re-render current state
    if published:
        state.mark_dirty()  # websites are public
    raise web.HTTPFound("/webapp/profile")


def add_routes(app: web.Application) -> None:
    app.add_routes([
        web.get("/webapp/ratings", ratings_page),
        web.post("/webapp/ratings/set", ratings_set),
        web.get("/webapp/reviews", reviews_page),
        web.get("/webapp/reviews/{book}", review_compose),
        web.post("/webapp/reviews", review_submit),
        web.get("/webapp/lists", lists_page),
        web.post("/webapp/lists/create", lists_create),
        web.post("/webapp/lists/act", lists_action),
        web.get("/webapp/profile", profile_page),
        web.post("/webapp/profile/act", profile_action),
    ])
