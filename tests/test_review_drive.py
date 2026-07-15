"""Review drive: candidate selection, allowlist gating, and the reply state machine."""

import asyncio
import json

from agent import bot, clubdb, commands, config, db, publish
from agent.club import review_drive as rd
from agent.mail.email_jmap import InboundEmail


def _msg(text, thread_id="T1", from_email="jamie@example.test"):
    return InboundEmail(
        id="m1", thread_id=thread_id, message_id="msg1@example.test",
        from_name="Jamie", from_email=from_email, to=["oliver@rwbookclub.com"],
        cc=[], reply_to=[], subject="Re: Your review of Blindsight?",
        text=text, received_at="2026-07-04T13:00:00Z", references=[])


def _rated_unreviewed(slug="jamie", book_slug="heart-of-darkness", rating=5):
    with db.connect() as conn:
        mid = clubdb.lookup_member_id(slug)
        bid = conn.execute("SELECT id FROM club_books WHERE slug=?", (book_slug,)).fetchone()[0]
        clubdb.upsert_review(conn, book_id=bid, member_id=mid, rating=rating, body=None)
    return mid


# ── Selection ────────────────────────────────────────────────────────────────
def test_candidate_prefers_high_rating_and_skips_reviewed(fresh_db, monkeypatch):
    mid = _rated_unreviewed(rating=5)
    with db.connect() as conn:
        bid2 = conn.execute("SELECT id FROM club_books WHERE slug='enshittification'").fetchone()[0]
        clubdb.upsert_review(conn, book_id=bid2, member_id=mid, rating=3, body=None)
    cand = rd.next_candidate("jamie")
    assert cand and cand["slug"] == "heart-of-darkness" and cand["rating"] == 5


def test_candidate_honors_caps_optout_and_inflight(fresh_db):
    mid = _rated_unreviewed()
    # per-book ask cap: the capped book is skipped (selection moves on, doesn't stop —
    # the fixture's jamie has other rated-unreviewed books, like the real one)
    for _ in range(rd.MAX_ASKS_PER_BOOK):
        db.record_event(actor="oliver", kind="review_requested", member_id=mid,
                        category="reading", detail=json.dumps({"book_slug": "heart-of-darkness"}),
                        occurred_at="2026-01-01 00:00:00")
    nxt = rd.next_candidate("jamie")
    assert nxt is not None and nxt["slug"] != "heart-of-darkness"
    # weekly cooldown (fresh recent ask on another book)
    with db.connect() as conn:
        conn.execute("DELETE FROM events WHERE kind='review_requested'")
    db.record_event(actor="oliver", kind="review_requested", member_id=mid,
                    category="reading", detail=json.dumps({"book_slug": "other"}))
    assert rd.next_candidate("jamie") is None
    # opt-out wins over everything
    with db.connect() as conn:
        conn.execute("DELETE FROM events WHERE kind='review_requested'")
    db.record_event(actor="member", kind="review_optout", member_id=mid, category="reading")
    assert rd.next_candidate("jamie") is None
    # in-flight draft blocks a new ask
    with db.connect() as conn:
        conn.execute("DELETE FROM events WHERE kind='review_optout'")
    db.create_review_draft(member_id=mid, book_slug="x", thread_id="T9")
    assert rd.next_candidate("jamie") is None


def test_allowlist_gates_everything(fresh_db, monkeypatch):
    _rated_unreviewed(slug="erik", book_slug="enshittification")
    monkeypatch.setattr(config, "REVIEW_DRIVE_MEMBERS", "jamie")
    assert "erik" not in rd.allowlisted_slugs()   # eligible but not allowlisted
    monkeypatch.setattr(config, "REVIEW_DRIVE_MEMBERS", "all")
    assert {"jamie", "erik"} <= rd.allowlisted_slugs()
    assert "oliver" not in rd.allowlisted_slugs()  # the agent never asks himself
    monkeypatch.setattr(config, "REVIEW_DRIVE_MEMBERS", "")
    assert rd.allowlisted_slugs() == set()         # empty = off


# ── State machine ────────────────────────────────────────────────────────────
def _draft(fresh_db, state="awaiting_reply", draft_json=None, rounds=0):
    mid = clubdb.lookup_member_id("jamie")
    did = db.create_review_draft(member_id=mid, book_slug="heart-of-darkness", thread_id="T1")
    db.update_review_draft(did, state=state, draft_json=draft_json, rounds=rounds)
    return db.draft_for_thread("T1")


def _mute_mail(monkeypatch):
    sent = []
    monkeypatch.setattr(rd.outbound, "send", lambda **kw: sent.append(kw) or {"emailId": "e"})
    monkeypatch.setattr(rd.oliver, "compose", lambda kind, facts, fallback="": fallback)
    return sent


def test_reply_extracts_and_asks_for_confirmation(fresh_db, monkeypatch):
    sent = _mute_mail(monkeypatch)
    monkeypatch.setattr(rd.oliver, "complete", lambda *a, **kw: json.dumps({
        "body": "A dark, riveting read.", "rating": 4, "recommend": True,
        "discussion": None, "quote": None, "declined": False, "stop_asking": False}))
    draft = _draft(fresh_db)
    rd.handle_reply(draft, _msg("Loved it. 4 stars. A dark, riveting read. I'd recommend it."))
    updated = db.draft_for_thread("T1")
    assert updated["state"] == "awaiting_confirm"
    assert json.loads(updated["draft_json"])["rating"] == 4
    assert "★★★★☆" in sent[0]["body"] and "A dark, riveting read." in sent[0]["body"]
    assert sent[0]["in_reply_to"] == "msg1@example.test"


def test_first_reply_keeps_the_rating_quoted_in_the_ask(fresh_db, monkeypatch):
    _rated_unreviewed(rating=5)
    fresh_db.link_member_email("jamie@example.test", "jamie")
    sent = []

    def send(**kw):
        sent.append(kw)
        return {"emailId": f"e{len(sent)}", "threadId": "T1"}

    monkeypatch.setattr(rd.outbound, "send", send)
    monkeypatch.setattr(rd.oliver, "compose", lambda kind, facts, fallback="": fallback)
    monkeypatch.setattr(rd.oliver, "complete", lambda *a, **kw: json.dumps({
        "body": "A dark, riveting read.", "rating": None, "recommend": None,
        "discussion": None, "quote": None, "declined": False, "stop_asking": False}))

    cand = rd.next_candidate("jamie")
    assert cand and cand["rating"] == 5
    rd.send_ask("jamie", cand)
    draft = db.draft_for_thread("T1")
    assert json.loads(draft["draft_json"])["rating"] == 5

    assert rd.handle_reply(draft, _msg("A dark, riveting read.")) is False
    updated = db.draft_for_thread("T1")
    assert json.loads(updated["draft_json"])["rating"] == 5
    assert "★★★★★" in sent[-1]["body"]


def test_confirmation_yes_writes_review(fresh_db, monkeypatch):
    sent = _mute_mail(monkeypatch)
    written = []
    monkeypatch.setattr(rd.reviews, "write_review",
                        lambda *a, **kw: written.append((a, kw)) or {"rating": 4})
    draft = _draft(fresh_db, state="awaiting_confirm", draft_json=json.dumps(
        {"body": "A dark, riveting read.", "rating": 4, "recommend": True}))
    assert rd.handle_reply(draft, _msg("Yes, looks good!")) is True
    assert written and written[0][0] == ("heart-of-darkness", "Jamie")
    assert written[0][1]["rating"] == "4" and written[0][1]["review"] == "A dark, riveting read."
    assert db.draft_for_thread("T1") is None  # state=written → no longer open
    with db.connect() as conn:
        assert conn.execute("SELECT 1 FROM events WHERE kind='review_recorded'").fetchone()
    assert "Recorded" in sent[0]["body"]


def test_confirmation_preserves_canonical_fields_missing_from_old_draft(fresh_db, monkeypatch):
    mid = _rated_unreviewed(rating=5)
    with db.connect() as conn:
        bid = conn.execute(
            "SELECT id FROM club_books WHERE slug='heart-of-darkness'").fetchone()[0]
        clubdb.upsert_review(
            conn, book_id=bid, member_id=mid, rating=5, discussion_quality=4,
            would_recommend=True, favorite_quote="The horror!", body=None)
    _mute_mail(monkeypatch)
    draft = _draft(fresh_db, state="awaiting_confirm", draft_json=json.dumps({
        "body": "A dark, riveting read.", "rating": None, "recommend": None,
        "discussion": None, "quote": None}))

    assert rd.handle_reply(draft, _msg("YES")) is True
    with db.connect() as conn:
        row = conn.execute(
            "SELECT rating, discussion_quality, would_recommend, favorite_quote, body "
            "FROM club_reviews WHERE member_id=? AND book_id=("
            "SELECT id FROM club_books WHERE slug='heart-of-darkness')", (mid,)).fetchone()
    assert dict(row) == {
        "rating": 5, "discussion_quality": 4, "would_recommend": 1,
        "favorite_quote": "The horror!", "body": "A dark, riveting read.",
    }


def test_inbound_confirmation_publishes_on_bot_event_loop(fresh_db, monkeypatch):
    """Production seam: reply handling is threaded, but task creation must stay on the bot loop."""
    mid = _rated_unreviewed(rating=5)
    fresh_db.link_member_email("jamie@example.test", "jamie")
    did = db.create_review_draft(
        member_id=mid, book_slug="heart-of-darkness", thread_id="T1",
        draft_json=json.dumps({"body": "A dark, riveting read.", "rating": 5,
                               "recommend": True, "discussion": None, "quote": None}),
    )
    db.update_review_draft(did, state="awaiting_confirm")
    _mute_mail(monkeypatch)
    seen = []
    deployed = []
    monkeypatch.setattr(bot.email_jmap, "mark_seen",
                        lambda email_id, answered=False: seen.append((email_id, answered)))
    monkeypatch.setattr(publish, "publish_site", lambda: deployed.append(True) or {"deployed": True})
    monkeypatch.setattr(commands, "_publisher_task", None)
    monkeypatch.setattr(commands, "_publish_dirty", False)

    async def run():
        await bot._handle_inbound_email(_msg("YES"))
        assert commands._publisher_task is not None
        await commands._publisher_task

    asyncio.run(run())

    assert db.email_processed("m1")
    assert seen == [("m1", True)]
    assert deployed == [True]
    assert db.draft_for_thread("T1") is None
    with db.connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM events WHERE kind='review_recorded' AND member_id=?", (mid,)
        ).fetchone()


def test_failed_review_retry_does_not_duplicate_receipt_ledger(fresh_db, monkeypatch):
    mid = _rated_unreviewed(rating=5)
    fresh_db.link_member_email("jamie@example.test", "jamie")
    db.create_review_draft(
        member_id=mid, book_slug="heart-of-darkness", thread_id="T1",
        draft_json=json.dumps({"body": "A dark, riveting read.", "rating": 5}),
    )
    monkeypatch.setattr(
        bot.inbound_email.review_drive, "handle_reply",
        lambda draft, msg: (_ for _ in ()).throw(RuntimeError("test failure")),
    )

    asyncio.run(bot._handle_inbound_email(_msg("YES")))
    asyncio.run(bot._handle_inbound_email(_msg("YES")))

    with db.connect() as conn:
        receipt_events = conn.execute(
            "SELECT COUNT(*) c FROM events WHERE kind='email_reply' AND source='email:m1'"
        ).fetchone()["c"]
        receipt_activity = conn.execute(
            "SELECT COUNT(*) c FROM activity_events WHERE kind='email_received'"
        ).fetchone()["c"]
        inbound = conn.execute(
            "SELECT status, error FROM inbound_emails WHERE email_id='m1'"
        ).fetchone()
    assert receipt_events == 1
    assert receipt_activity == 1
    assert inbound["status"] == "failed" and "test failure" in inbound["error"]


def test_corrections_reextract_and_two_round_park(fresh_db, monkeypatch):
    sent = _mute_mail(monkeypatch)
    monkeypatch.setattr(rd.oliver, "complete", lambda *a, **kw: json.dumps(
        {"body": "Corrected text.", "rating": 5, "recommend": None,
         "discussion": None, "quote": None, "declined": False, "stop_asking": False}))
    draft = _draft(fresh_db, state="awaiting_confirm",
                   draft_json=json.dumps({"body": "old", "rating": 4}), rounds=0)
    rd.handle_reply(draft, _msg("Actually make it 5 stars"))
    assert db.draft_for_thread("T1")["rounds"] == 1
    rd.handle_reply(db.draft_for_thread("T1"), _msg("hmm, tweak the wording again"))
    assert db.draft_for_thread("T1")["rounds"] == 2
    rd.handle_reply(db.draft_for_thread("T1"), _msg("one more change"))
    assert db.draft_for_thread("T1") is None  # parked → closed
    assert "my-club" in sent[-1]["body"]


def test_stop_asking_records_optout_and_memory(fresh_db, monkeypatch):
    _mute_mail(monkeypatch)
    monkeypatch.setattr(rd.oliver, "complete", lambda *a, **kw: json.dumps(
        {"body": "", "rating": None, "recommend": None, "discussion": None,
         "quote": None, "declined": True, "stop_asking": True}))
    draft = _draft(fresh_db)
    rd.handle_reply(draft, _msg("please stop sending me these"))
    with db.connect() as conn:
        assert conn.execute("SELECT 1 FROM events WHERE kind='review_optout'").fetchone()
    mems = db.get_memories(subject="jamie", source="member_request")
    assert any("review-request" in m["note"] for m in mems)


def test_never_infers_rating(fresh_db, monkeypatch):
    """The extraction prompt contract, pinned: glowing text without a stated number → null."""
    sent = _mute_mail(monkeypatch)
    monkeypatch.setattr(rd.oliver, "complete", lambda *a, **kw: json.dumps(
        {"body": "Absolutely loved it, best book in years.", "rating": None,
         "recommend": None, "discussion": None, "quote": None,
         "declined": False, "stop_asking": False}))
    draft = _draft(fresh_db)
    rd.handle_reply(draft, _msg("Absolutely loved it, best book in years."))
    assert "(no rating stated)" in sent[0]["body"]
    # and the SYSTEM prompt itself carries the rule
    assert "NEVER infer a rating from tone" in rd._EXTRACT_SYSTEM
