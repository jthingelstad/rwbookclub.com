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


def _safe_return(form, default: str) -> str:
    """A redirect target from the form, restricted to in-app paths (no open redirect). Must be a
    single-slash `/webapp/` path: reject `/webapp//evil.com` (some proxies treat `//` as
    scheme-relative) and backslash variants browsers normalize to `//`."""
    ret = (form.get("return") or "").strip()
    if not ret.startswith("/webapp/") or ret.startswith("/webapp//") or "\\" in ret:
        return default
    return ret


def apply_identity_op(slug: str, op: str, val: str, label: str | None = None,
                      new_value: str | None = None) -> bool:
    """Apply one profile identity mutation for `slug`. Shared by the member profile page
    and the admin member editor. Returns True when the change is public (websites).

    URL scheme safety (rejecting javascript:/data:/… so nothing dangerous reaches an href on the
    public site) is enforced in db.link_member_website / db.update_member_website, which raise
    ValueError — callers catch it. `new_value` is only used by edit-website (the new URL); `val`
    is the existing URL being edited."""
    if op == "add-website" and val:
        db.link_member_website(val, slug, linked_by="webapp", label=label)
        return True
    if op == "edit-website" and val:
        db.update_member_website(val, slug, url=new_value or val, label=label)
        return True
    if op == "remove-website" and val:
        db.remove_member_website(val, slug)
        return True
    if op == "add-email" and val:
        db.link_member_email(val, slug, linked_by="webapp")
    elif op == "add-phone" and val:
        db.link_member_sms(val, slug, linked_by="webapp")
    elif op == "remove-phone" and val:
        db.remove_member_sms(val, slug)
    elif op == "primary-website" and val:
        db.set_primary_identity(slug, "website", val)
        return True  # website order is public
    elif op == "primary-email" and val:
        db.set_primary_identity(slug, "email", val)
    elif op == "primary-phone" and val:
        db.set_primary_identity(slug, "sms", val)
    return False


def _load_books() -> list[dict]:
    with db.connect() as conn:
        books = clubdb.all_books(conn)
    return sorted(({"slug": b["slug"], "title": b["title"]} for b in books),
                  key=lambda b: b["title"].lower())


def _member_ratings(member_slug: str) -> dict[str, dict]:
    with db.connect() as conn:
        return {r["book_slug"]: r for r in clubdb.all_reviews(conn)
                if r["member_slug"] == member_slug}


def _book_dates() -> dict[str, str]:
    """Map book slug → the date it was most recently discussed (its meeting date).
    Drives reverse-chronological ordering and the year filter on Ratings/Reviews."""
    with db.connect() as conn:
        return {r["slug"]: r["date"] for r in conn.execute(
            "SELECT b.slug AS slug, MAX(m.date) AS date FROM club_meeting_books mb "
            "JOIN club_books b ON b.id = mb.book_id "
            "JOIN club_meetings m ON m.id = mb.meeting_id "
            "WHERE m.date IS NOT NULL GROUP BY b.id")}


def _by_discussed_desc(books: list[dict], dates: dict[str, str]) -> list[dict]:
    """Annotate each book with its discussed date/year and sort newest-first (undated last)."""
    rows = []
    for b in books:
        d = dates.get(b["slug"])
        rows.append({**b, "date": d, "year": (d or "")[:4] or None})
    rows.sort(key=lambda r: (r["date"] or ""), reverse=True)
    return rows


# ── Ratings — bulk grid ──────────────────────────────────────────────────────
async def ratings_page(request: web.Request) -> web.Response:
    slug = request["session"]["slug"]
    books = await asyncio.to_thread(_load_books)
    mine = await asyncio.to_thread(_member_ratings, slug)
    dates = await asyncio.to_thread(_book_dates)
    rows = [{
        "slug": b["slug"], "title": b["title"],
        "rating": (mine.get(b["slug"]) or {}).get("rating"),
        "dnf": bool((mine.get(b["slug"]) or {}).get("dnf")),
    } for b in books]
    rows = _by_discussed_desc(rows, dates)  # most-recently-discussed first
    years = sorted({r["year"] for r in rows if r["year"]}, reverse=True)
    return render("ratings.html", request, books=rows, years=years)


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
    dates = await asyncio.to_thread(_book_dates)
    books = _by_discussed_desc(books, dates)  # most-recently-discussed first
    reviewed = {s for s, r in mine.items() if (r.get("body") or r.get("rating") or r.get("dnf"))}
    years = sorted({b["year"] for b in books if b["year"]}, reverse=True)
    return render("reviews_index.html", request, books=books, reviewed=reviewed, years=years)


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
# Two pages: the index (manage lists — create/rename/delete) and a per-list detail page (manage the
# books on one list). The shared item ops live in lists_action.
async def lists_page(request: web.Request) -> web.Response:
    slug = request["session"]["slug"]
    all_lists = await asyncio.to_thread(cr.lists)
    mine = [lst for lst in all_lists if lst.get("owner") == slug]
    return render("lists.html", request, lists=mine)


async def list_detail(request: web.Request) -> web.Response:
    slug = request["session"]["slug"]
    list_slug = request.match_info["slug"]
    all_lists = await asyncio.to_thread(cr.lists)
    lst = next((x for x in all_lists if x.get("slug") == list_slug and x.get("owner") == slug), None)
    if lst is None:
        return web.Response(status=404, text="No such list.")
    books = await asyncio.to_thread(_load_books)
    titles = {b["slug"]: b["title"] for b in books}
    return render("list_detail.html", request, lst=lst, books=books, titles=titles)


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
    raise web.HTTPFound(_safe_return(form, "/webapp/lists"))


async def lists_action(request: web.Request) -> web.Response:
    """POST /webapp/lists/act — add-book / remove-book / edit / delete / set-note / reorder,
    dispatched by `op`. AJAX callers (drag-reorder, inline note) get JSON; forms get a redirect."""
    form = _form(request)
    session = request["session"]
    actor, is_admin = session["slug"], bool(session.get("a"))
    op = form.get("op")
    ref = form.get("list", "")
    err = None
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
        elif op == "set-note":
            await asyncio.to_thread(lists_writer.set_note, ref, form.get("book", ""),
                                    form.get("note"), actor_slug=actor, is_admin=is_admin)
        elif op == "reorder":
            slugs = [s for s in (form.get("order") or "").split(",") if s]
            await asyncio.to_thread(lists_writer.reorder, ref, slugs, actor_slug=actor, is_admin=is_admin)
    except lists_writer.ListError as e:
        err = str(e)
    if err is None:
        state.mark_dirty()
    if request.headers.get("X-Requested-With") == "fetch":
        return web.json_response({"ok": err is None, "error": err}, status=200 if err is None else 400)
    raise web.HTTPFound(_safe_return(form, "/webapp/lists"))


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
    label = (form.get("label") or "").strip() or None
    new_value = (form.get("new_value") or "").strip() or None
    try:
        published = await asyncio.to_thread(apply_identity_op, slug, op, val, label, new_value)
    except ValueError:
        published = False  # bad input — re-render current state
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
        web.get("/webapp/lists/{slug}", list_detail),
        web.get("/webapp/profile", profile_page),
        web.post("/webapp/profile/act", profile_action),
    ])
