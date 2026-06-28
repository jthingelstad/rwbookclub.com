"""Admin-facing routes: edit book data, add/edit meetings, change hosts. Gated by the admin flag in
the session (enforced in the server middleware for any /webapp/admin path). Reuses `clubdb.upsert_book`
/ `corpus_write.schedule_meeting` and the new `clubdb.update_meeting`/`set_meeting_hosts` writers."""

from __future__ import annotations

import asyncio
import json

from aiohttp import web

from agent import clubdb, corpus_gen, corpus_write, db
from agent import corpus_read as cr
from agent.club import openlibrary
from agent.webapp import state
from agent.webapp.render import render
from agent.webapp.routes_member import apply_identity_op
from corpus.paths import DATA_DIR


def _form(request: web.Request):
    return request.get("form") or {}


def _members() -> list[dict]:
    with db.connect() as conn:
        return [{"slug": m["slug"], "name": m["name"], "id": m["id"], "current": bool(m["is_current"])}
                for m in clubdb.all_members(conn)]


# ── Book data ────────────────────────────────────────────────────────────────
async def books_page(request: web.Request) -> web.Response:
    return await _render_books(request)


def _all_books() -> list[dict]:
    with db.connect() as conn:
        return clubdb.all_books(conn)


def _load_book_core(slug: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM club_books WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            return None
        authors = [r["name"] for r in conn.execute(
            "SELECT a.name FROM club_book_authors ba JOIN club_authors a ON a.id = ba.author_id "
            "WHERE ba.book_id = ? ORDER BY ba.ordinal", (row["id"],))]
        pickers = clubdb.book_picker_slugs(conn, row["id"])
    d = dict(row)
    d["authors"] = authors
    d["pickers"] = pickers
    d["subjects"] = json.loads(row["subjects_json"]) if row["subjects_json"] else []
    return d


async def book_edit(request: web.Request) -> web.Response:
    slug = request.match_info["slug"]
    book = await asyncio.to_thread(_load_book_core, slug)
    if book is None:
        return web.Response(status=404, text="No such book.")
    members = await asyncio.to_thread(_members)
    return render("admin_book.html", request, book=book, topics=clubdb.TOPICS, members=members)


def _save_book(slug: str, form, picker_slugs: list[str]) -> str | None:
    core = _load_book_core(slug)
    if core is None:
        return "no such book"
    topic = (form.get("topic") or "").strip() or None
    if topic is not None and topic not in clubdb.TOPICS:
        return "invalid topic"

    def _int(name):
        v = (form.get(name) or "").strip()
        return int(v) if v.isdigit() else None

    authors = [a.strip() for a in (form.get("authors") or "").split(",") if a.strip()]
    meta = {
        "title": core["title"],  # read-only: keeps the slug stable
        "subtitle": (form.get("subtitle") or "").strip() or None,
        "topic": topic,
        "fiction": form.get("fiction") in ("1", "true", "on"),
        "publicationYear": _int("publication_year"),
        "pageCount": _int("page_count"),
        "isbn13": (form.get("isbn13") or "").strip() or None,
        "olKey": (form.get("ol_key") or "").strip() or None,
        "synopsis": (form.get("synopsis") or "").strip() or None,
        "subjects": core["subjects"],          # preserve — not edited here
        "authors": authors or core["authors"],  # preserve if the field was cleared
    }
    with db.connect() as conn:
        res = clubdb.upsert_book(conn, meta)
        picker_ids = [p for p in (clubdb.member_id_for_slug(conn, s) for s in picker_slugs if s) if p]
        clubdb.set_book_pickers(conn, res["id"], picker_ids)
        corpus_gen.write_book_file(conn, res["id"], DATA_DIR)
    return None


async def book_save(request: web.Request) -> web.Response:
    slug = request.match_info["slug"]
    form = _form(request)
    pickers = form.getall("pickers", []) if hasattr(form, "getall") else []
    err = await asyncio.to_thread(_save_book, slug, form, pickers)
    if err:
        book = await asyncio.to_thread(_load_book_core, slug)
        return render("admin_book.html", request, status=400, book=book or {"slug": slug},
                      topics=clubdb.TOPICS, error=err)
    state.mark_dirty()
    raise web.HTTPFound("/webapp/admin/books")


async def _render_books(request: web.Request, *, status: int = 200, error: str | None = None):
    with_conn = await asyncio.to_thread(lambda: [
        {"slug": b["slug"], "title": b["title"], "topic": b.get("topic")} for b in _all_books()])
    return render("admin_books.html", request, status=status, error=error,
                  books=sorted(with_conn, key=lambda b: b["title"].lower()))


def _add_book(title: str, isbn: str | None) -> dict | None:
    """Look the book up on Open Library and write it to the club record. Returns the
    write result (with slug) or None if nothing matched."""
    meta = openlibrary.lookup(title, isbn)
    if not meta or not meta.get("title"):
        return None
    return corpus_write.write_book(meta)  # upsert + enrich + corpus files


async def book_add(request: web.Request) -> web.Response:
    form = _form(request)
    title = (form.get("title") or "").strip()
    isbn = (form.get("isbn") or "").strip() or None
    if not title:
        return await _render_books(request, status=400, error="A book needs a title.")
    try:
        res = await asyncio.to_thread(_add_book, title, isbn)
    except Exception:
        return await _render_books(request, status=500, error="Something went wrong adding that book.")
    if res is None:
        return await _render_books(
            request, status=404,
            error=f"Couldn't find “{title}” on Open Library — try an ISBN, or check the title.")
    state.mark_dirty()
    # Land on the edit page so the admin can refine the fetched metadata.
    raise web.HTTPFound(f"/webapp/admin/books/{res['slug']}")


# ── Meetings ─────────────────────────────────────────────────────────────────
def _all_meetings() -> list[dict]:
    with db.connect() as conn:
        return clubdb.all_meetings(conn)


async def meetings_page(request: web.Request) -> web.Response:
    meetings = await asyncio.to_thread(_all_meetings)
    books = await asyncio.to_thread(lambda: sorted(
        ({"slug": b["slug"], "title": b["title"]} for b in _all_books()), key=lambda b: b["title"].lower()))
    members = await asyncio.to_thread(_members)
    meetings = sorted(meetings, key=lambda m: (m.get("date") or ""), reverse=True)
    return render("admin_meetings.html", request, meetings=meetings, books=books,
                  members=members, meeting_types=clubdb.MEETING_TYPES)


def _add_meeting(date: str, book_slugs: list[str], picker_slug: str | None,
                 host_slugs: list[str], types: list[str]) -> bool:
    day = (date or "").strip()[:10]
    if len(day) != 10:
        return False
    types = [t for t in types if t in clubdb.MEETING_TYPES]
    with db.connect() as conn:
        book_ids = [b for b in (clubdb.book_id_for_slug(conn, s) for s in book_slugs if s) if b]
        use_types = types or (["Book"] if book_ids else ["Social"])
        mid = clubdb.create_meeting(conn, date_iso=day, book_id=None,
                                    types=use_types, placeholder=True)
        if book_ids:
            clubdb.set_meeting_books(conn, mid, book_ids)
        if picker_slug:
            pid = clubdb.member_id_for_slug(conn, picker_slug)
            for bid in book_ids if pid else []:
                clubdb.set_book_picker(conn, bid, pid)
        host_ids = [h for h in (clubdb.member_id_for_slug(conn, s) for s in host_slugs if s) if h]
        if host_ids:
            clubdb.set_meeting_hosts(conn, mid, host_ids)
        for bid in book_ids:
            corpus_gen.write_book_file(conn, bid, DATA_DIR)
        corpus_gen.write_meeting_file(conn, mid, DATA_DIR)
    return True


async def meeting_add(request: web.Request) -> web.Response:
    form = _form(request)
    books = form.getall("books", []) if hasattr(form, "getall") else []
    hosts = form.getall("hosts", []) if hasattr(form, "getall") else []
    types = form.getall("types", []) if hasattr(form, "getall") else []
    created = await asyncio.to_thread(_add_meeting, form.get("date", ""), books,
                                      form.get("picker") or None, hosts, types)
    if created:
        state.mark_dirty()
    raise web.HTTPFound("/webapp/admin/meetings")


def _meeting_by_id(meeting_id: int) -> dict | None:
    with db.connect() as conn:
        return next((m for m in clubdb.all_meetings(conn) if m["id"] == meeting_id), None)


def _sorted_books() -> list[dict]:
    return sorted(({"slug": b["slug"], "title": b["title"]} for b in _all_books()),
                  key=lambda b: b["title"].lower())


def _meeting_book_pickers(book_slugs: list[str]) -> dict[str, list[str]]:
    with db.connect() as conn:
        out = {}
        for slug in book_slugs:
            bid = clubdb.book_id_for_slug(conn, slug)
            out[slug] = clubdb.book_picker_slugs(conn, bid) if bid else []
    return out


async def meeting_edit(request: web.Request) -> web.Response:
    mid = int(request.match_info["id"])
    meeting = await asyncio.to_thread(_meeting_by_id, mid)
    if meeting is None:
        return web.Response(status=404, text="No such meeting.")
    members = await asyncio.to_thread(_members)
    books = await asyncio.to_thread(_sorted_books)
    pickers = await asyncio.to_thread(_meeting_book_pickers, meeting["book_slugs"])
    titles = {b["slug"]: b["title"] for b in books}
    return render("admin_meeting.html", request, meeting=meeting, members=members, books=books,
                  titles=titles, book_pickers=pickers, meeting_types=clubdb.MEETING_TYPES)


def _save_meeting(meeting_id: int, form, host_slugs: list[str], book_slugs: list[str],
                  picker_slugs: list[str], types: list[str]) -> None:
    types = [t for t in types if t in clubdb.MEETING_TYPES]
    # books[i] is picked by pickers[i] — the edit form emits one of each per book row.
    pairs = list(zip(book_slugs, picker_slugs + [""] * len(book_slugs)))
    with db.connect() as conn:
        clubdb.update_meeting(
            conn, meeting_id,
            date=(form.get("date") or "").strip() or None,
            start_time=(form.get("start_time") or "").strip() or None,
            location=(form.get("location") or "").strip() or None,
            notes=(form.get("notes") or "").strip() or None,
            types=types or None,
            placeholder=False if form.get("held") in ("1", "true", "on") else None,
        )
        # The edit form is the source of truth for books + hosts — set both (empty clears).
        host_ids = [h for h in (clubdb.member_id_for_slug(conn, s) for s in host_slugs if s) if h]
        clubdb.set_meeting_hosts(conn, meeting_id, host_ids)
        book_ids = []
        for bslug, pslug in pairs:
            bid = clubdb.book_id_for_slug(conn, bslug) if bslug else None
            if bid is None:
                continue
            book_ids.append(bid)
            pid = clubdb.member_id_for_slug(conn, pslug) if pslug else None
            clubdb.set_book_pickers(conn, bid, [pid] if pid else [])
        clubdb.set_meeting_books(conn, meeting_id, book_ids)
        for bid in book_ids:
            corpus_gen.write_book_file(conn, bid, DATA_DIR)
        corpus_gen.write_meeting_file(conn, meeting_id, DATA_DIR)


async def meeting_save(request: web.Request) -> web.Response:
    mid = int(request.match_info["id"])
    form = _form(request)
    host_slugs = form.getall("hosts", []) if hasattr(form, "getall") else []
    book_slugs = form.getall("books", []) if hasattr(form, "getall") else []
    picker_slugs = form.getall("pickers", []) if hasattr(form, "getall") else []
    types = form.getall("types", []) if hasattr(form, "getall") else []
    await asyncio.to_thread(_save_meeting, mid, form, host_slugs, book_slugs, picker_slugs, types)
    state.mark_dirty()
    raise web.HTTPFound("/webapp/admin/meetings")


# ── Members ──────────────────────────────────────────────────────────────────
async def members_page(request: web.Request) -> web.Response:
    members = await asyncio.to_thread(_members)
    members = sorted(members, key=lambda m: ((not m["current"]), m["name"].lower()))
    return render("admin_members.html", request, members=members)


def _member_action(op: str, name: str, slug: str) -> bool:
    with db.connect() as conn:
        if op == "add" and name.strip():
            clubdb.create_member(conn, name)
            return True
        if op == "retire" and slug:
            return clubdb.set_member_current(conn, slug, is_current=False)
        if op == "reactivate" and slug:
            return clubdb.set_member_current(conn, slug, is_current=True)
    return False


async def member_action(request: web.Request) -> web.Response:
    form = _form(request)
    changed = await asyncio.to_thread(_member_action, form.get("op", ""),
                                      form.get("name", ""), form.get("slug", ""))
    if changed:
        state.mark_dirty()  # member currency drives whole swaths of the site
    raise web.HTTPFound("/webapp/admin/members")


def _member_by_slug(slug: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT slug, name, is_current FROM club_members WHERE slug = ?",
                           (slug,)).fetchone()
    return {"slug": row["slug"], "name": row["name"], "current": bool(row["is_current"])} if row else None


def _member_identities(slug: str) -> dict:
    return {
        "websites": db.member_handles(slug, "website"),
        "emails": db.member_handles(slug, "email"),
        "phones": db.member_handles(slug, "sms"),
    }


async def member_edit(request: web.Request) -> web.Response:
    slug = request.match_info["slug"]
    member = await asyncio.to_thread(_member_by_slug, slug)
    if member is None:
        return web.Response(status=404, text="No such member.")
    ids = await asyncio.to_thread(_member_identities, slug)
    return render("admin_member.html", request, member=member, **ids)


def _member_save(slug: str, op: str, form) -> bool:
    """Apply one admin edit to `slug`. Returns True if the public site should rebuild."""
    if op == "rename":
        with db.connect() as conn:
            clubdb.rename_member(conn, slug, form.get("name", ""))
        return True  # display name is public
    if op == "retire":
        with db.connect() as conn:
            clubdb.set_member_current(conn, slug, is_current=False)
        return True
    if op == "reactivate":
        with db.connect() as conn:
            clubdb.set_member_current(conn, slug, is_current=True)
        return True
    # Otherwise it's an identity op (website/email/phone), shared with the member profile page.
    val = (form.get("value") or "").strip()
    label = (form.get("label") or "").strip() or None
    return apply_identity_op(slug, op or "", val, label)


async def member_save(request: web.Request) -> web.Response:
    slug = request.match_info["slug"]
    form = _form(request)
    if await asyncio.to_thread(_member_by_slug, slug) is None:
        return web.Response(status=404, text="No such member.")
    try:
        published = await asyncio.to_thread(_member_save, slug, form.get("op", ""), form)
    except ValueError:
        published = False  # bad input — re-render current state
    if published:
        state.mark_dirty()
    raise web.HTTPFound(f"/webapp/admin/members/{slug}")


# ── Club lists ───────────────────────────────────────────────────────────────
async def lists_page(request: web.Request) -> web.Response:
    all_lists = await asyncio.to_thread(cr.lists)
    club = [lst for lst in all_lists if lst.get("scope") == "club"]
    books = await asyncio.to_thread(_sorted_books)
    titles = {b["slug"]: b["title"] for b in books}
    return render("admin_lists.html", request, lists=club, books=books, titles=titles)


def add_routes(app: web.Application) -> None:
    app.add_routes([
        web.get("/webapp/admin/books", books_page),
        web.post("/webapp/admin/books/add", book_add),
        web.get("/webapp/admin/books/{slug}", book_edit),
        web.post("/webapp/admin/books/{slug}", book_save),
        web.get("/webapp/admin/meetings", meetings_page),
        web.post("/webapp/admin/meetings/add", meeting_add),
        web.get("/webapp/admin/meetings/{id}", meeting_edit),
        web.post("/webapp/admin/meetings/{id}", meeting_save),
        web.get("/webapp/admin/members", members_page),
        web.post("/webapp/admin/members", member_action),
        web.get("/webapp/admin/members/{slug}", member_edit),
        web.post("/webapp/admin/members/{slug}", member_save),
        web.get("/webapp/admin/lists", lists_page),
    ])
