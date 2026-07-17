"""Book Cloud: no-dedupe storage, backdated seeding, per-title aggregation, and the
ctx-derived-mentioner rule on the tools."""

from __future__ import annotations

import json

import pytest

from agent import db
from agent.tools import dispatch


def test_add_requires_title_and_reason(fresh_db):
    with pytest.raises(ValueError):
        db.add_book_cloud_entry(title="X", reason="   ", surface="discord")
    with pytest.raises(ValueError):
        db.add_book_cloud_entry(title="", reason="why", surface="discord")


def test_in_turn_reason_taxonomy_matches_approved_vocabulary():
    from agent.tools import TOOLS

    definition = next(tool for tool in TOOLS if tool["name"] == "book_cloud_add")
    reason_kinds = definition["input_schema"]["properties"]["reason_kind"]["enum"]
    assert reason_kinds == [
        "nomination",
        "recommendation",
        "comparison",
        "caution",
        "context",
        "inquiry",
        "joke",
    ]


@pytest.mark.parametrize("reason_kind", ["inquiry", "caution"])
def test_revised_reason_kinds_round_trip(fresh_db, reason_kind):
    result = json.loads(
        dispatch(
            "book_cloud_add",
            {
                "title": "Piranesi",
                "reason": "title-specific future pick signal",
                "reason_kind": reason_kind,
            },
            {"member_slug": "jamie", "speaker": "Jamie", "channel_id": "123"},
        )
    )
    assert result["saved"] is True
    assert db.recent_book_cloud()[0]["reason_kind"] == reason_kind


def test_no_dedupe_and_aggregation(fresh_db):
    db.add_book_cloud_entry(
        title="Seeing Like a State",
        reason="Tom recommended it again",
        surface="discord",
        mentioned_by="tom",
        created_at="2024-01-01 00:00:00",
    )
    db.add_book_cloud_entry(
        title="seeing like a state",
        reason="compared to The Dawn of Everything",
        surface="mailing_list",
        mentioned_by="jamie",
        created_at="2026-02-01 00:00:00",
    )
    raw = db.recent_book_cloud()
    assert len(raw) == 2  # two reasons = two rows, never merged
    agg = db.book_cloud_titles()
    assert len(agg) == 1  # ...but one orbit-view row
    row = agg[0]
    assert row["mention_count"] == 2
    assert row["first_mentioned"].startswith("2024-01-01")  # backdated seeding keeps history true
    assert row["last_mentioned"].startswith("2026-02-01")
    assert row["mentioners"] == ["jamie", "tom"]
    assert len(row["recentReasons"]) == 2


def test_titles_member_filter(fresh_db):
    db.add_book_cloud_entry(title="A", reason="r1", surface="discord", mentioned_by="tom")
    db.add_book_cloud_entry(title="B", reason="r2", surface="discord", mentioned_by="jamie")
    only_tom = db.book_cloud_titles(member="tom")
    assert [r["title"] for r in only_tom] == ["A"]


def test_book_cloud_add_dispatch_uses_ctx_not_input(fresh_db):
    # Mentioner/surface/provenance come from ctx; the model cannot spoof them via input.
    out = json.loads(
        dispatch(
            "book_cloud_add",
            {
                "title": "The Power Broker",
                "reason": "compared to Seeing Like a State",
                "reason_kind": "comparison",
                "mentioned_by": "SPOOF",
            },  # ignored — not in schema
            {
                "member_slug": "jamie",
                "speaker": "Jamie",
                "channel_id": "email:list:t1",
                "source_message_id": "m9",
            },
        )
    )
    assert out["saved"] is True
    row = db.recent_book_cloud()[0]
    assert row["mentioned_by"] == "jamie"  # from ctx
    assert row["surface"] == "mailing_list"  # derived from channel_id
    assert row["reason_kind"] == "comparison"


def test_book_cloud_add_links_corpus_slug_when_read(fresh_db):
    # A mention of a book the club HAS read gets book_slug set (fixture corpus has Watchmen).
    dispatch(
        "book_cloud_add",
        {"title": "Watchmen", "reason": "cited as the club's comics outlier"},
        {"member_slug": "erik", "speaker": "Erik", "channel_id": "123"},
    )
    assert db.recent_book_cloud()[0]["book_slug"] == "watchmen"


def test_recent_book_cloud_member_and_kind_filters(fresh_db):
    db.add_book_cloud_entry(
        title="A", reason="r1", surface="discord", mentioned_by="tom", reason_kind="nomination"
    )
    db.add_book_cloud_entry(
        title="B", reason="r2", surface="discord", mentioned_by="jamie", reason_kind="joke"
    )
    assert [r["title"] for r in db.recent_book_cloud(member="tom")] == ["A"]
    assert [r["title"] for r in db.recent_book_cloud(kind="joke")] == ["B"]
    assert db.recent_book_cloud(member="tom", kind="joke") == []


def test_admin_bookcloud_view_helper(fresh_db):
    from agent.webapp import routes_oliver_pages as routes_admin

    db.add_book_cloud_entry(
        title="Piranesi", reason="light fiction lead", surface="discord", mentioned_by="loren"
    )
    db.add_book_cloud_entry(
        title="Watchmen",
        reason="mention of a read book",
        surface="discord",
        book_slug="watchmen",
        mentioned_by="tom",
        reason_kind="comparison",
    )
    titles = routes_admin._bookcloud_view(
        view="titles", q="", member="", kind="", unread=True, limit=50
    )
    assert [r["title"] for r in titles["rows"]] == ["Piranesi"]  # read title filtered
    everything = routes_admin._bookcloud_view(
        view="titles", q="", member="", kind="", unread=False, limit=50
    )
    read_flags = {r["title"]: r["isRead"] for r in everything["rows"]}
    assert read_flags == {"Piranesi": False, "Watchmen": True}  # flagged, not hidden
    raw = routes_admin._bookcloud_view(
        view="mentions", q="", member="tom", kind="comparison", unread=True, limit=50
    )
    assert [r["title"] for r in raw["rows"]] == ["Watchmen"]  # raw view: member+kind filters
    assert routes_admin._bookcloud_kinds() == ["comparison"]


def test_admin_bookcloud_template_renders(fresh_db):
    from agent.webapp.render import _env

    db.add_book_cloud_entry(
        title="Piranesi",
        author="Susanna Clarke",
        reason="suggested as light fiction",
        surface="mailing_list",
        mentioned_by="loren",
        reason_kind="recommendation",
    )
    tpl = _env.get_template("admin_bookcloud.html")
    common = {
        "is_admin": True,
        "csrf": "t",
        "member_name": "Jamie",
        "publish_pending": False,
        "members": [{"slug": "loren", "name": "Loren", "current": True}],
        "kinds": ["recommendation"],
    }
    html = tpl.render(
        rows=db.book_cloud_titles(),
        f={"view": "titles", "q": "", "member": "", "kind": "", "unread": True, "limit": 200},
        **common,
    )
    assert "Piranesi" in html and "suggested as light fiction" in html and "loren" in html
    html2 = tpl.render(
        rows=db.recent_book_cloud(),
        f={"view": "mentions", "q": "", "member": "", "kind": "", "unread": True, "limit": 200},
        **common,
    )
    assert "Piranesi" in html2 and "mailing_list" in html2


def test_book_cloud_recent_dispatch_modes(fresh_db):
    db.add_book_cloud_entry(title="A", reason="r1", surface="discord")
    db.add_book_cloud_entry(title="A", reason="r2", surface="discord")
    ctx = {"member_slug": "jamie", "speaker_user_id": "u1"}
    raw = json.loads(dispatch("book_cloud_recent", {}, ctx))
    assert len(raw) == 2
    agg = json.loads(dispatch("book_cloud_recent", {"titles": True}, ctx))
    assert len(agg) == 1 and agg[0]["mention_count"] == 2
