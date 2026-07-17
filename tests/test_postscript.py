"""Postscript — the after-meeting digest: candidate selection + rotation, prompt grounding, the
raised web-search budget, and the once-per-meeting post-meeting trigger (bounded window + dedup)."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from agent import clock, db, oliver, proactive
from agent.club import meeting_emails as me


def test_web_search_budget_override():
    def cap(tools):
        return [t for t in tools if t.get("name") == "web_search"][0]["max_uses"]

    assert cap(oliver._tools_for(None)) == 3  # default untouched for normal chat
    assert cap(oliver._tools_for(10)) == 10  # raised for the digest


def test_candidate_selection_anchors_and_spans_eras(monkeypatch):
    books = [
        {
            "slug": "anchor",
            "title": "Anchor",
            "authors": ["A"],
            "isRead": True,
            "meetingDate": "2026-06-01",
            "fiction": True,
        },
        {
            "slug": "distant",
            "title": "Distant",
            "authors": ["B"],
            "isRead": True,
            "meetingDate": "2005-01-01",
        },
        {
            "slug": "middle",
            "title": "Middle",
            "authors": ["C"],
            "isRead": True,
            "meetingDate": "2015-01-01",
        },
        {
            "slug": "recent",
            "title": "Recent",
            "authors": ["D"],
            "isRead": True,
            "meetingDate": "2025-01-01",
        },
        {"slug": "upcoming", "title": "Up", "authors": ["E"], "isRead": False, "meetingDate": None},
    ]
    monkeypatch.setattr(me.cr, "books", lambda: books)
    monkeypatch.setattr(
        me.cr, "get_author", lambda name: {"website": None, "notableWorks": None, "deathYear": None}
    )
    slugs = [c["slug"] for c in me.select_postscript_candidates("anchor", exclude=set())]
    assert slugs[0] == "anchor"  # the just-discussed book always leads
    assert "upcoming" not in slugs  # not-yet-read is excluded
    assert {"distant", "middle", "recent"} <= set(slugs)  # spans all eras, not just recent


def test_selection_caps_per_author(monkeypatch):
    # One prolific author across many reads shouldn't dominate the pool.
    books = [
        {
            "slug": f"b{i}",
            "title": f"B{i}",
            "authors": ["Prolific"],
            "isRead": True,
            "meetingDate": f"20{10 + i}-01-01",
        }
        for i in range(6)
    ]
    monkeypatch.setattr(me.cr, "books", lambda: books)
    monkeypatch.setattr(
        me.cr, "get_author", lambda name: {"deathYear": None, "website": "x", "notableWorks": None}
    )
    picks = me.select_postscript_candidates(None, exclude=set())
    assert 0 < len(picks) <= me._MAX_PER_AUTHOR  # capped despite six eligible


def test_candidate_facts_and_prompt_carry_links(monkeypatch):
    books = [
        {
            "slug": "klara",
            "title": "Klara and the Sun",
            "authors": ["Kazuo Ishiguro"],
            "isRead": True,
            "meetingDate": "2021-05-01",
            "olKey": "/works/OL1W",
            "topic": "SF",
        }
    ]
    monkeypatch.setattr(me.cr, "books", lambda: books)
    monkeypatch.setattr(
        me.cr,
        "get_author",
        lambda name: {
            "website": "https://ishiguro.example",
            "notableWorks": ["The Remains of the Day"],
            "deathYear": None,
        },
    )
    c = me.select_postscript_candidates(None, exclude=set())[0]
    assert c["clubUrl"].endswith("/books/klara/")  # internal club-page link
    assert c["externalUrl"] == "https://openlibrary.org/works/OL1W"  # external reference
    p = me.postscript_prompt([c], anchor_title="Klara and the Sun")
    assert (
        "/books/klara/" in p and "markdown links" in p and "<cite>" in p
    )  # link-not-cite guidance
    assert "TANGENT" in p  # tangents invited


def test_selection_excludes_recently_featured(monkeypatch):
    books = [
        {"slug": "a", "title": "A", "authors": ["X"], "isRead": True, "meetingDate": "2026-01-01"},
        {"slug": "b", "title": "B", "authors": ["Y"], "isRead": True, "meetingDate": "2026-02-01"},
    ]
    monkeypatch.setattr(me.cr, "books", lambda: books)
    monkeypatch.setattr(me.cr, "get_author", lambda name: None)
    assert [c["slug"] for c in me.select_postscript_candidates(None, exclude={"b"})] == ["a"]


def test_recent_featured_slugs_parses(fresh_db):
    db.record_group_event(1, me.POSTSCRIPT_KIND, detail=json.dumps({"featured": ["a", "b"]}))
    db.record_group_event(2, me.POSTSCRIPT_KIND, detail=json.dumps({"featured": ["c"]}))
    assert me._recent_featured_slugs() == {"a", "b", "c"}


def test_postscript_prompt_has_grounding_and_candidates():
    cands = [
        {
            "slug": "x",
            "title": "Klara and the Sun",
            "authors": ["Kazuo Ishiguro"],
            "yearRead": "2021",
            "picker": "Jamie",
            "fiction": True,
            "authorWebsite": None,
            "authorNotableWorks": ["The Remains of the Day"],
        }
    ]
    p = me.postscript_prompt(cands, anchor_title="Klara and the Sun")
    assert "Klara and the Sun" in p and "Kazuo Ishiguro" in p  # candidate + author fed in
    assert "The Remains of the Day" in p  # notable works → search targeting
    assert "web_search" in p and "LEAVE IT OUT" in p  # grounding rails
    assert "<email>" in p and "## " in p  # format contract


def test_extract_email_strips_cite_tags():
    # web_search citation markers must never reach a member's inbox.
    raw = (
        '<email>Pressfield published <cite index="76-3,73-6">*The Arcadian*</cite> in 2026.</email>'
    )
    out = me._extract_email(raw)
    assert out == "Pressfield published *The Arcadian* in 2026."
    assert "<cite" not in out and "</cite>" not in out


def _anchor_book():
    return {
        "slug": "recent",
        "title": "Recent",
        "meetingDate": "2026-06-01",
        "meetingStartTime": "18:30",
    }


def test_maybe_send_postscript_fires_once_in_window(monkeypatch, fresh_db):
    monkeypatch.setattr(me, "_most_recent_read_book", _anchor_book)
    monkeypatch.setattr(proactive.clubdb, "meeting_id_for_book_slug", lambda slug: 42)
    monkeypatch.setattr(
        me,
        "postscript_email",
        lambda anchor=None: {"subject": "Postscript", "body": "hi", "offered": ["recent", "x"]},
    )
    sent = []

    async def fake_send(subject, body, **_kwargs):
        sent.append((subject, body))

    monkeypatch.setattr(proactive, "send_club_email", fake_send)

    start = clock.meeting_start("2026-06-01", "18:30")
    now = start + timedelta(days=8)  # inside [+7, +10]
    assert asyncio.run(proactive._maybe_send_postscript(now)) == 1
    assert len(sent) == 1
    assert db.has_group_event(42, me.POSTSCRIPT_KIND)  # dedup event recorded
    assert me._recent_featured_slugs() == {"recent", "x"}  # offered slugs stored for rotation
    # A second tick in the same window does not re-send.
    assert asyncio.run(proactive._maybe_send_postscript(now)) == 0
    assert len(sent) == 1


def test_maybe_send_postscript_outside_window(monkeypatch, fresh_db):
    monkeypatch.setattr(me, "_most_recent_read_book", _anchor_book)
    monkeypatch.setattr(proactive.clubdb, "meeting_id_for_book_slug", lambda slug: 43)
    fired = []
    monkeypatch.setattr(me, "postscript_email", lambda anchor=None: fired.append(1) or {})
    start = clock.meeting_start("2026-06-01", "18:30")
    assert (
        asyncio.run(proactive._maybe_send_postscript(start + timedelta(days=3))) == 0
    )  # too early
    assert (
        asyncio.run(proactive._maybe_send_postscript(start + timedelta(days=20))) == 0
    )  # too late
    assert fired == []  # never even drafted outside the window
