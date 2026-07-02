"""Book Cloud: no-dedupe storage, backdated seeding, per-title aggregation, and the
ctx-derived-mentioner rule on the tools."""

from __future__ import annotations

import json

from agent import db
from agent.tools import dispatch


def test_add_requires_title_and_reason(fresh_db):
    import pytest
    with pytest.raises(ValueError):
        db.add_book_cloud_entry(title="X", reason="   ", surface="discord")
    with pytest.raises(ValueError):
        db.add_book_cloud_entry(title="", reason="why", surface="discord")


def test_no_dedupe_and_aggregation(fresh_db):
    db.add_book_cloud_entry(title="Seeing Like a State", reason="Tom recommended it again",
                            surface="discord", mentioned_by="tom", created_at="2024-01-01 00:00:00")
    db.add_book_cloud_entry(title="seeing like a state", reason="compared to The Dawn of Everything",
                            surface="mailing_list", mentioned_by="jamie",
                            created_at="2026-02-01 00:00:00")
    raw = db.recent_book_cloud()
    assert len(raw) == 2                                     # two reasons = two rows, never merged
    agg = db.book_cloud_titles()
    assert len(agg) == 1                                     # ...but one orbit-view row
    row = agg[0]
    assert row["mention_count"] == 2
    assert row["first_mentioned"].startswith("2024-01-01")   # backdated seeding keeps history true
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
    out = json.loads(dispatch("book_cloud_add",
                              {"title": "The Power Broker", "reason": "compared to Seeing Like a State",
                               "reason_kind": "comparison",
                               "mentioned_by": "SPOOF"},           # ignored — not in schema
                              {"member_slug": "jamie", "speaker": "Jamie",
                               "channel_id": "email:list:t1", "source_message_id": "m9"}))
    assert out["saved"] is True
    row = db.recent_book_cloud()[0]
    assert row["mentioned_by"] == "jamie"                     # from ctx
    assert row["surface"] == "mailing_list"                   # derived from channel_id
    assert row["reason_kind"] == "comparison"


def test_book_cloud_add_links_corpus_slug_when_read(fresh_db):
    # A mention of a book the club HAS read gets book_slug set (fixture corpus has Watchmen).
    dispatch("book_cloud_add", {"title": "Watchmen", "reason": "cited as the club's comics outlier"},
             {"member_slug": "erik", "speaker": "Erik", "channel_id": "123"})
    assert db.recent_book_cloud()[0]["book_slug"] == "watchmen"


def test_book_cloud_recent_dispatch_modes(fresh_db):
    db.add_book_cloud_entry(title="A", reason="r1", surface="discord")
    db.add_book_cloud_entry(title="A", reason="r2", surface="discord")
    raw = json.loads(dispatch("book_cloud_recent", {}, {}))
    assert len(raw) == 2
    agg = json.loads(dispatch("book_cloud_recent", {"titles": True}, {}))
    assert len(agg) == 1 and agg[0]["mention_count"] == 2
