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
        clubdb.update_meeting(conn, mid, location="Jamie's place", notes="bring snacks", placeholder=False)
        clubdb.set_meeting_hosts(conn, mid, [jamie])
        after = next(m for m in clubdb.all_meetings(conn) if m["id"] == mid)
    assert after["location"] == "Jamie's place" and after["notes"] == "bring snacks"
    assert after["placeholder"] == 0 and "jamie" in after["host_slugs"]


def test_topics_constant():
    assert "Technology" in clubdb.TOPICS and len(clubdb.TOPICS) == 11


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
        finally:
            async with server._lock:
                if server._runner is not None:
                    await server._do_stop()

    asyncio.run(scenario())
    with db.connect() as conn:
        bid = clubdb.book_id_for_slug(conn, "heart-of-darkness")
        row = conn.execute("SELECT rating FROM club_reviews WHERE book_id=? AND member_id=?",
                           (bid, _jamie_id())).fetchone()
    assert row["rating"] == 4


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
