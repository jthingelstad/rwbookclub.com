"""Web app: tokens (mint/resolve/consume), signed sessions, on-demand lifecycle, and the new
clubdb writers the routes depend on (set_rating / update_meeting / set_meeting_hosts)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import aiohttp

from agent import clubdb, config, db, webapp
from agent.webapp import server, sessions


def _jamie_id() -> int:
    mid = clubdb.lookup_member_id("jamie")
    assert mid is not None
    return mid


# ── Tokens ───────────────────────────────────────────────────────────────────
def test_mint_and_resolve_round_trip():
    token = webapp.mint_token(_jamie_id(), is_admin=True)
    member = webapp.resolve_token(token)
    assert member["slug"] == "jamie" and member["name"] == "Jamie" and member["is_admin"] is True


def test_member_scope_is_not_admin_by_default():
    assert webapp.resolve_token(webapp.mint_token(_jamie_id(), is_admin=False))["is_admin"] is False


def test_unknown_and_empty_token_resolve_to_none():
    assert webapp.resolve_token("nope-not-a-real-token") is None
    assert webapp.resolve_token(None) is None
    assert webapp.resolve_token("") is None


def test_expired_token_resolves_to_none():
    assert webapp.resolve_token(webapp.mint_token(_jamie_id(), is_admin=False,
                                                  ttl=timedelta(seconds=-1))) is None


def test_consume_token_is_single_use():
    token = webapp.mint_token(_jamie_id(), is_admin=False)
    assert webapp.consume_token(token)["slug"] == "jamie"   # first use works
    assert webapp.consume_token(token) is None              # second use rejected
    assert webapp.resolve_token(token) is None              # and it's now spent


# ── Signed session cookies ───────────────────────────────────────────────────
def test_session_sign_verify_tamper_and_expiry():
    member = {"member_id": _jamie_id(), "slug": "jamie", "name": "Jamie", "is_admin": True}
    cookie = sessions.make_session(member)
    payload = sessions.read_session(cookie)
    assert payload["slug"] == "jamie" and payload["a"] == 1 and payload["csrf"]
    # tamper → reject
    raw, _, sig = cookie.partition(".")
    assert sessions.read_session(raw + "." + sig[:-2] + "xx") is None
    assert sessions.read_session("garbage") is None
    # expired → reject
    expired = sessions.make_session(member)
    monkey = sessions._SESSION_TTL
    try:
        sessions._SESSION_TTL = timedelta(seconds=-1)
        assert sessions.read_session(sessions.make_session(member)) is None
    finally:
        sessions._SESSION_TTL = monkey
    assert sessions.read_session(expired) is not None  # the un-expired one still verifies


def test_csrf_ok():
    sess = {"csrf": "abc123"}
    assert sessions.csrf_ok(sess, "abc123") is True
    assert sessions.csrf_ok(sess, "wrong") is False
    assert sessions.csrf_ok(sess, None) is False


def test_website_rejects_dangerous_schemes():
    import pytest
    # http(s) and scheme-less (→ https) are accepted and stored.
    db.link_member_website("https://blog.example.com", "jamie", linked_by="t")
    db.link_member_website("example.org/path", "jamie", linked_by="t")  # gets https:// prefix
    stored = {h["identifier"] for h in db.member_handles("jamie", "website")}
    assert "https://blog.example.com" in stored and "https://example.org/path" in stored
    # javascript:/data: must be rejected even when crafted to carry "://" and a dotted host —
    # otherwise it renders as a clickable XSS href on the public member page.
    for bad in ("javascript://%0aalert(document.domain)//x.com",
                "javascript:alert(1)", "data:text/html,<script>alert(1)</script>",
                "ftp://example.com"):
        with pytest.raises(ValueError):
            db.link_member_website(bad, "jamie", linked_by="t")


def test_update_member_website_renames_and_repoints(fresh_db):
    db.link_member_website("https://old.example.com", "jamie", linked_by="t", label="Blog")
    # rename only — same URL, new label
    assert db.update_member_website("https://old.example.com", "jamie", label="My Blog") is True
    h = db.member_handles("jamie", "website")[0]
    assert h["identifier"] == "https://old.example.com" and h["label"] == "My Blog"
    # change the URL and clear the label in one edit
    assert db.update_member_website("https://old.example.com", "jamie",
                                    url="https://new.example.com", label="") is True
    h = db.member_handles("jamie", "website")[0]
    assert h["identifier"] == "https://new.example.com" and h["label"] is None
    # an unknown old URL changes nothing
    assert db.update_member_website("https://missing.example.com", "jamie", label="x") is False


def test_update_member_website_rejects_bad_url_and_collision(fresh_db):
    import pytest
    db.link_member_website("https://a.example.com", "jamie", linked_by="t")
    db.link_member_website("https://b.example.com", "jamie", linked_by="t")
    with pytest.raises(ValueError):  # XSS scheme rejected, same as add
        db.update_member_website("https://a.example.com", "jamie", url="javascript:alert(1)")
    with pytest.raises(ValueError):  # collide with the member's other website
        db.update_member_website("https://a.example.com", "jamie", url="https://b.example.com")


def test_delete_event(fresh_db):
    eid = db.record_event(actor="oliver", kind="note", category="club", detail="to-delete")
    assert any(e["id"] == eid for e in db.timeline(limit=50))
    assert db.delete_event(eid) is True
    assert all(e["id"] != eid for e in db.timeline(limit=50))
    assert db.delete_event(eid) is False  # already gone → no-op


def test_new_webapp_templates_render():
    """The split lists pages and reworked events page render without Jinja errors."""
    from agent.webapp.render import _env
    base_ctx = {"csrf": "tok", "member_name": "Jamie", "is_admin": True}
    lst = {"slug": "my-list", "name": "My List", "description": "desc",
           "books": [{"book": "heart-of-darkness", "note": "great"}]}
    books = [{"slug": "heart-of-darkness", "title": "Heart of Darkness"}]
    titles = {"heart-of-darkness": "Heart of Darkness"}
    detail = _env.get_template("list_detail.html").render(lst=lst, books=books, titles=titles, **base_ctx)
    assert "My List" in detail and "Heart of Darkness" in detail and "remove-book" in detail
    admin_detail = _env.get_template("admin_list_detail.html").render(
        lst=lst, books=books, titles=titles, **base_ctx)
    assert "My List" in admin_detail
    index = _env.get_template("lists.html").render(lists=[lst], **base_ctx)
    assert "Manage items" in index and "/webapp/lists/my-list" in index
    events = [{"id": 7, "occurred_at": "2026-06-01", "category": "club", "kind": "note",
               "member_name": None, "actor": "oliver", "detail": "hello", "surface": "system"}]
    page = _env.get_template("admin_events.html").render(
        events=events, categories=["club"], members=[], query_string="category=club",
        f={"category": "club", "member": "", "since": "", "until": "", "limit": 200}, **base_ctx)
    assert "ev-detail" in page and "hello" in page and "/webapp/admin/events/delete" in page


def test_webapp_refuses_dev_secret(monkeypatch):
    import pytest
    # Fail closed: the public server must never bind while signing sessions with the dev literal.
    monkeypatch.setattr(config, "WEBAPP_SECRET", config.WEBAPP_DEV_SECRET)
    monkeypatch.setattr(server, "_runner", None)
    with pytest.raises(RuntimeError, match="WEBAPP_SECRET"):
        asyncio.run(server.ensure_running())


def test_safe_return_blocks_open_redirect():
    from agent.webapp.routes_member import _safe_return
    # legitimate in-app paths pass through
    assert _safe_return({"return": "/webapp/admin/lists"}, "/webapp/lists") == "/webapp/admin/lists"
    assert _safe_return({"return": "/webapp/profile"}, "/webapp/lists") == "/webapp/profile"
    # off-site / scheme-relative / backslash variants fall back to the default
    for evil in ("//evil.com", "https://evil.com", "/webapp//evil.com",
                 "/webapp/\\evil.com", "", "/etc/passwd"):
        assert _safe_return({"return": evil}, "/webapp/lists") == "/webapp/lists"


# ── New clubdb writers ───────────────────────────────────────────────────────
def test_set_rating_preserves_existing_review_body():
    with db.connect() as conn:
        book_id = clubdb.book_id_for_slug(conn, "heart-of-darkness")
        member_id = _jamie_id()
        clubdb.upsert_review(conn, book_id=book_id, member_id=member_id, rating=3, body="loved it")
        clubdb.set_rating(conn, book_id, member_id, rating=5, dnf=False)
        row = conn.execute("SELECT rating, dnf, body FROM club_reviews WHERE book_id=? AND member_id=?",
                           (book_id, member_id)).fetchone()
    assert row["rating"] == 5 and row["dnf"] == 0 and row["body"] == "loved it"


def test_set_rating_creates_row_when_absent():
    with db.connect() as conn:
        book_id = clubdb.book_id_for_slug(conn, "enshittification")
        member_id = _jamie_id()
        rid = clubdb.set_rating(conn, book_id, member_id, rating=None, dnf=True)
        row = conn.execute("SELECT id, rating, dnf, body FROM club_reviews WHERE id=?", (rid,)).fetchone()
    assert row["dnf"] == 1 and row["rating"] is None and row["body"] is None


def test_update_meeting_and_set_hosts():
    with db.connect() as conn:
        meeting = clubdb.all_meetings(conn)[0]
        mid = meeting["id"]
        jamie = _jamie_id()
        clubdb.update_meeting(conn, mid, location="Jamie's place", notes="bring snacks")
        clubdb.set_meeting_hosts(conn, mid, [jamie])
        after = next(m for m in clubdb.all_meetings(conn) if m["id"] == mid)
    assert after["location"] == "Jamie's place" and after["notes"] == "bring snacks"
    assert "jamie" in after["host_slugs"]


def test_topics_constant():
    assert "Technology" in clubdb.TOPICS and len(clubdb.TOPICS) == 11


def test_meeting_id_or_404_rejects_non_int():
    import pytest
    from agent.webapp import routes_admin

    class _Req:
        def __init__(self, val):
            self.match_info = {"id": val}

    with pytest.raises(aiohttp.web.HTTPNotFound):  # non-numeric id → 404, not a raw 500
        routes_admin._meeting_id_or_404(_Req("not-a-number"))
    assert routes_admin._meeting_id_or_404(_Req("42")) == 42


def test_events_view_filters():
    from agent.webapp import routes_admin
    jamie = _jamie_id()
    db.record_event(actor="admin", kind="note", category="social", member_id=jamie,
                    detail="game night", occurred_at="2026-05-01 12:00:00")
    db.record_event(actor="admin", kind="note", category="club",
                    detail="anniversary", occurred_at="2026-05-02 12:00:00")
    # category filter
    social = routes_admin._load_events("social", "", "", "", 100)
    assert social and all(e["category"] == "social" for e in social)
    # member filter resolves slug → id; club-wide event is excluded
    mine = routes_admin._load_events("", "jamie", "", "", 100)
    assert all(e["member_id"] == jamie for e in mine)
    assert any(e["detail"] == "game night" for e in mine)
    # date window
    windowed = routes_admin._load_events("", "", "2026-05-02", "2026-05-03", 100)
    assert all("2026-05-02" <= e["occurred_at"][:10] <= "2026-05-03" for e in windowed)
    # categories list reflects real data
    assert "social" in routes_admin._event_categories()


def test_meeting_save_pairs_books_and_pickers():
    from agent.webapp import routes_admin
    with db.connect() as conn:
        mid = clubdb.all_meetings(conn)[0]["id"]
    # one book row → one picker; parallel arrays as the edit form posts them
    form = {"date": "2026-07-01", "start_time": "18:00", "held": "1"}
    routes_admin._save_meeting(mid, form, host_slugs=["jamie"],
                               book_slugs=["heart-of-darkness"], picker_slugs=["erik"],
                               types=["Book"])
    with db.connect() as conn:
        m = next(x for x in clubdb.all_meetings(conn) if x["id"] == mid)
        bid = clubdb.book_id_for_slug(conn, "heart-of-darkness")
        assert m["book_slugs"] == ["heart-of-darkness"] and m["host_slugs"] == ["jamie"]
        assert clubdb.book_picker_slugs(conn, bid) == ["erik"]
    # clearing the picker (explicit "— none —") removes it
    routes_admin._save_meeting(mid, form, host_slugs=["jamie"],
                               book_slugs=["heart-of-darkness"], picker_slugs=[""],
                               types=["Book"])
    with db.connect() as conn:
        bid = clubdb.book_id_for_slug(conn, "heart-of-darkness")
        assert clubdb.book_picker_slugs(conn, bid) == []


def test_set_book_pickers_multi():
    with db.connect() as conn:
        bid = clubdb.book_id_for_slug(conn, "heart-of-darkness")
        ids = [clubdb.member_id_for_slug(conn, s) for s in ("jamie", "erik")]
        clubdb.set_book_pickers(conn, bid, ids)
        assert clubdb.book_picker_slugs(conn, bid) == ["jamie", "erik"]
        clubdb.set_book_pickers(conn, bid, [])
        assert clubdb.book_picker_slugs(conn, bid) == []


def test_create_and_retire_member():
    with db.connect() as conn:
        res = clubdb.create_member(conn, "Test Person")
        assert res["slug"] == "test-person"
        assert next(m for m in clubdb.all_members(conn) if m["slug"] == "test-person")["is_current"] == 1
        assert clubdb.set_member_current(conn, "test-person", is_current=False) is True
        assert next(m for m in clubdb.all_members(conn) if m["slug"] == "test-person")["is_current"] == 0


def test_rename_member_keeps_slug():
    with db.connect() as conn:
        res = clubdb.create_member(conn, "Rename Me")
        slug = res["slug"]
        assert clubdb.rename_member(conn, slug, "Renamed") is True
        m = next(m for m in clubdb.all_members(conn) if m["slug"] == slug)
        assert m["name"] == "Renamed" and m["slug"] == slug  # slug is identity; must not move


def test_reorder_list_books_keeps_unmentioned():
    with db.connect() as conn:
        jamie = _jamie_id()
        lst = clubdb.create_list(conn, name="Reorder Test", scope="member", owner_id=jamie)
        b1 = clubdb.book_id_for_slug(conn, "heart-of-darkness")
        b2 = clubdb.book_id_for_slug(conn, "enshittification")
        clubdb.set_list_book(conn, lst["id"], b1)
        clubdb.set_list_book(conn, lst["id"], b2)
        clubdb.reorder_list_books(conn, lst["id"], [b2, b1])
        order = [r["book_id"] for r in conn.execute(
            "SELECT book_id FROM club_list_books WHERE list_id=? ORDER BY ordinal", (lst["id"],))]
        assert order == [b2, b1]
        # a stale order with only one id keeps the other at the end (no row dropped)
        clubdb.reorder_list_books(conn, lst["id"], [b1])
        order2 = [r["book_id"] for r in conn.execute(
            "SELECT book_id FROM club_list_books WHERE list_id=? ORDER BY ordinal", (lst["id"],))]
        assert order2 == [b1, b2]


def test_move_list_book_preserves_notes():
    with db.connect() as conn:
        jamie = _jamie_id()
        lst = clubdb.create_list(conn, name="Order Test", scope="member", owner_id=jamie)
        b1 = clubdb.book_id_for_slug(conn, "heart-of-darkness")
        b2 = clubdb.book_id_for_slug(conn, "enshittification")
        clubdb.set_list_book(conn, lst["id"], b1, "note-a")
        clubdb.set_list_book(conn, lst["id"], b2, "note-b")
        assert clubdb.move_list_book(conn, lst["id"], b2, up=True) is True
        rows = conn.execute("SELECT book_id, note FROM club_list_books WHERE list_id=? ORDER BY ordinal",
                            (lst["id"],)).fetchall()
        assert [(r["book_id"], r["note"]) for r in rows] == [(b2, "note-b"), (b1, "note-a")]


def test_member_handles_and_set_primary():
    db.link_member_email("a@x.com", "jamie", linked_by="t")
    db.link_member_email("b@x.com", "jamie", linked_by="t")
    assert {h["identifier"] for h in db.member_handles("jamie", "email")} >= {"a@x.com", "b@x.com"}
    assert db.set_primary_identity("jamie", "email", "b@x.com") is True
    by = {h["identifier"]: h["is_primary"] for h in db.member_handles("jamie", "email")}
    assert by["b@x.com"] is True and by["a@x.com"] is False
    assert db.set_primary_identity("jamie", "email", "nobody@x.com") is False  # not this member's


def test_create_bookless_meeting_and_set_books():
    with db.connect() as conn:
        mid = clubdb.create_meeting(conn, date_iso="2026-09-01", book_id=None, types=["Social"])
        assert next(m for m in clubdb.all_meetings(conn) if m["id"] == mid)["book_slugs"] == []
        b1 = clubdb.book_id_for_slug(conn, "heart-of-darkness")
        b2 = clubdb.book_id_for_slug(conn, "enshittification")
        clubdb.set_meeting_books(conn, mid, [b1, b2])  # two books, ordered
        got = next(m for m in clubdb.all_meetings(conn) if m["id"] == mid)["book_slugs"]
        assert got == ["heart-of-darkness", "enshittification"]
        clubdb.set_meeting_books(conn, mid, [])        # back to bookless
        assert next(m for m in clubdb.all_meetings(conn) if m["id"] == mid)["book_slugs"] == []


# ── End-to-end route smoke (in-process server, real templates) ───────────────
def test_routes_end_to_end(monkeypatch):
    monkeypatch.setattr(config, "WEBAPP_PORT", 8798)
    monkeypatch.setattr(server, "_IDLE_TIMEOUT", timedelta(minutes=10))
    monkeypatch.setattr(server, "_CHECK_INTERVAL", 5)
    base = "http://127.0.0.1:8798"
    jamie = _jamie_id()
    # Build the session cookie directly (aiohttp's jar won't send a Secure cookie over plain http).
    sess_val = sessions.make_session({"member_id": jamie, "slug": "jamie", "name": "Jamie", "is_admin": False})
    csrf = sessions.read_session(sess_val)["csrf"]
    hdr = {"Cookie": f"{sessions.COOKIE_NAME}={sess_val}"}

    async def scenario():
        published = {"n": 0}
        # Don't actually rebuild the site during the test.
        import agent.webapp.server as srv
        monkeypatch.setattr(srv, "_trigger_publish", lambda: published.__setitem__("n", published["n"] + 1))
        try:
            await webapp.ensure_running()
            async with aiohttp.ClientSession() as s:
                # a chat link-preview bot must NOT spend the single-use token
                botua = {"User-Agent": "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discord.com)"}
                ptok = webapp.mint_token(jamie, is_admin=False)
                async with s.get(f"{base}/webapp?t={ptok}", headers=botua, allow_redirects=False) as r:
                    assert r.status == 200  # neutral page, not a redirect — token untouched
                async with s.get(f"{base}/webapp?t={ptok}", allow_redirects=False) as r:
                    assert r.status == 302  # the real tap still exchanges it
                # token exchange → 302 + Set-Cookie
                tok = webapp.mint_token(jamie, is_admin=False)
                async with s.get(f"{base}/webapp?t={tok}", allow_redirects=False) as r:
                    assert r.status == 302
                    assert any("oliver_session=" in c for c in r.headers.getall("Set-Cookie", []))
                # member pages render
                for path, needle in [("/webapp/ratings", "Rate books"),
                                     ("/webapp/reviews", "Write a review"),
                                     ("/webapp/lists", "Your lists"),
                                     ("/webapp/profile", "Your profile")]:
                    async with s.get(base + path, headers=hdr) as r:
                        assert r.status == 200, path
                        assert needle in await r.text(), path
                # admin route refused for a non-admin session
                async with s.get(base + "/webapp/admin/books", headers=hdr) as r:
                    assert r.status == 403
                # an ADMIN session can load every admin page (exercises the DB-in-thread paths)
                admin_val = sessions.make_session(
                    {"member_id": jamie, "slug": "jamie", "name": "Jamie", "is_admin": True})
                ahdr = {"Cookie": f"{sessions.COOKIE_NAME}={admin_val}"}
                with db.connect() as conn:
                    mid = clubdb.all_meetings(conn)[0]["id"]
                for path, needle in [("/webapp/admin/books", "Edit book data"),
                                     ("/webapp/admin/books/heart-of-darkness", "Open Library key"),
                                     ("/webapp/admin/meetings", "Schedule a meeting"),
                                     (f"/webapp/admin/meetings/{mid}", "Host(s)")]:
                    async with s.get(base + path, headers=ahdr) as r:
                        assert r.status == 200, path
                        assert needle in await r.text(), path
                # admin can create a two-book meeting and a bookless meeting
                acsrf = sessions.read_session(admin_val)["csrf"]
                async with s.post(base + "/webapp/admin/meetings/add", headers=ahdr, allow_redirects=False,
                                  data=[("csrf", acsrf), ("date", "2026-10-01"),
                                        ("books", "heart-of-darkness"), ("books", "enshittification")]) as r:
                    assert r.status == 302
                async with s.post(base + "/webapp/admin/meetings/add", headers=ahdr, allow_redirects=False,
                                  data=[("csrf", acsrf), ("date", "2026-10-02")]) as r:
                    assert r.status == 302
                # set two pickers on a book via the admin editor
                async with s.post(base + "/webapp/admin/books/enshittification", headers=ahdr, allow_redirects=False,
                                  data=[("csrf", acsrf), ("pickers", "jamie"), ("pickers", "erik")]) as r:
                    assert r.status == 302
                # member management: add then retire
                async with s.post(base + "/webapp/admin/members", headers=ahdr, allow_redirects=False,
                                  data={"csrf": acsrf, "op": "add", "name": "Web Test Member"}) as r:
                    assert r.status == 302
                async with s.post(base + "/webapp/admin/members", headers=ahdr, allow_redirects=False,
                                  data={"csrf": acsrf, "op": "retire", "slug": "web-test-member"}) as r:
                    assert r.status == 302
                # no session at all → 401-ish (expired page)
                async with s.get(base + "/webapp/ratings", allow_redirects=False) as r:
                    assert r.status == 401
                # AJAX rating with CSRF writes through
                ajax = dict(hdr, **{"X-Requested-With": "fetch"})
                async with s.post(base + "/webapp/ratings/set", headers=ajax,
                                  data={"book_slug": "heart-of-darkness", "rating": "4", "csrf": csrf}) as r:
                    assert r.status == 200 and (await r.json())["ok"] is True
                # bad CSRF rejected
                async with s.post(base + "/webapp/ratings/set", headers=ajax,
                                  data={"book_slug": "heart-of-darkness", "rating": "4", "csrf": "bad"}) as r:
                    assert r.status == 403
                # publish endpoint runs the (patched) trigger
                async with s.post(base + "/webapp/publish", headers=ajax, data={"csrf": csrf}) as r:
                    assert r.status == 200
                # lists: create, add two books, reorder
                async with s.post(base + "/webapp/lists/create", headers=hdr, allow_redirects=False,
                                  data={"csrf": csrf, "name": "E2E List"}) as r:
                    assert r.status == 302
                for bk in ("heart-of-darkness", "enshittification"):
                    async with s.post(base + "/webapp/lists/act", headers=hdr, allow_redirects=False,
                                      data={"csrf": csrf, "op": "add-book", "list": "jamie-e2e-list", "book": bk}) as r:
                        assert r.status == 302
                async with s.post(base + "/webapp/lists/act", headers=hdr, allow_redirects=False,
                                  data={"csrf": csrf, "op": "move-up", "list": "jamie-e2e-list", "book": "enshittification"}) as r:
                    assert r.status == 302
                # profile: add two emails, make the second primary
                for em in ("one@x.com", "two@x.com"):
                    async with s.post(base + "/webapp/profile/act", headers=hdr, allow_redirects=False,
                                      data={"csrf": csrf, "op": "add-email", "value": em}) as r:
                        assert r.status == 302
                async with s.post(base + "/webapp/profile/act", headers=hdr, allow_redirects=False,
                                  data={"csrf": csrf, "op": "primary-email", "value": "two@x.com"}) as r:
                    assert r.status == 302
                # profile: add a website, then EDIT its name + URL (the reported bug)
                async with s.post(base + "/webapp/profile/act", headers=hdr, allow_redirects=False,
                                  data={"csrf": csrf, "op": "add-website", "label": "Blog",
                                        "value": "https://old.example.com"}) as r:
                    assert r.status == 302
                async with s.post(base + "/webapp/profile/act", headers=hdr, allow_redirects=False,
                                  data={"csrf": csrf, "op": "edit-website", "value": "https://old.example.com",
                                        "label": "My Blog", "new_value": "https://new.example.com"}) as r:
                    assert r.status == 302
                # lists: the index links into a per-list detail page that manages its books
                async with s.get(base + "/webapp/lists/jamie-e2e-list", headers=hdr) as r:
                    assert r.status == 200
                    assert "Heart of Darkness" in await r.text()
                # admin: events page offers a Delete control; deleting removes the row
                evid = await asyncio.to_thread(
                    db.record_event, actor="oliver", kind="note", category="club", detail="webtest-event")
                async with s.get(base + "/webapp/admin/events", headers=ahdr) as r:
                    assert r.status == 200 and "Delete" in await r.text()
                async with s.post(base + "/webapp/admin/events/delete", headers=ahdr, allow_redirects=False,
                                  data={"csrf": acsrf, "id": str(evid)}) as r:
                    assert r.status == 302
                # admin: the club-list detail page renders
                async with s.get(base + "/webapp/admin/lists/books-of-the-year", headers=ahdr) as r:
                    assert r.status == 200 and "Books of the Year" in await r.text()
        finally:
            async with server._lock:
                if server._runner is not None:
                    await server._do_stop()

    asyncio.run(scenario())
    with db.connect() as conn:
        bid = clubdb.book_id_for_slug(conn, "heart-of-darkness")
        row = conn.execute("SELECT rating FROM club_reviews WHERE book_id=? AND member_id=?",
                           (bid, _jamie_id())).fetchone()
        meetings = {m["date"]: m for m in clubdb.all_meetings(conn)}
        pickers = clubdb.book_picker_slugs(conn, clubdb.book_id_for_slug(conn, "enshittification"))
        wtm = [m for m in clubdb.all_members(conn) if m["slug"] == "web-test-member"]
        e2e = next(x for x in clubdb.all_lists(conn) if x["slug"] == "jamie-e2e-list")
    assert row["rating"] == 4
    assert meetings["2026-10-01"]["book_slugs"] == ["heart-of-darkness", "enshittification"]
    assert meetings["2026-10-02"]["book_slugs"] == []  # bookless meeting created
    assert pickers == ["jamie", "erik"]                # two pickers set via the book editor
    assert wtm and wtm[0]["is_current"] == 0           # member added then retired
    assert [e["book_slug"] for e in e2e["entries"]] == ["enshittification", "heart-of-darkness"]  # reordered
    primary = next((h["identifier"] for h in db.member_handles("jamie", "email") if h["is_primary"]), None)
    assert primary == "two@x.com"
    sites = {h["identifier"]: h["label"] for h in db.member_handles("jamie", "website")}
    assert sites == {"https://new.example.com": "My Blog"}     # renamed + re-pointed in place
    assert all(e["detail"] != "webtest-event" for e in db.timeline(limit=500))  # event deleted


# ── On-demand lifecycle (in-process) ─────────────────────────────────────────
def test_server_starts_on_demand_and_stops_when_idle(monkeypatch):
    monkeypatch.setattr(config, "WEBAPP_PORT", 8799)
    monkeypatch.setattr(server, "_IDLE_TIMEOUT", timedelta(seconds=0))
    monkeypatch.setattr(server, "_CHECK_INTERVAL", 0.05)

    async def scenario():
        try:
            await webapp.ensure_running()
            assert server._runner is not None
            async with aiohttp.ClientSession() as s, s.get("http://127.0.0.1:8799/healthz") as r:
                assert r.status == 200
            for _ in range(40):
                await asyncio.sleep(0.05)
                if server._runner is None:
                    break
            assert server._runner is None, "server should idle-shutdown when unused"
        finally:
            async with server._lock:
                if server._runner is not None:
                    await server._do_stop()

    asyncio.run(scenario())
