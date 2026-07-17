"""Book Cloud archive seeding: extraction → backdated inserts, read-title skip, parse safety."""

from __future__ import annotations

import json

from agent import db
from agent.script.archive import mine_archive_book_cloud as miner


def _mail(mid: str, slug: str, sent_at: str, body: str):
    db.upsert_mail_message(
        {
            "message_id": mid,
            "thread_id": f"t-{mid}",
            "from_email": f"{slug}@x.com",
            "from_name": slug.title(),
            "member_slug": slug,
            "subject": "Books",
            "sent_at": sent_at,
            "received_at": sent_at,
            "body_text": body,
            "body_clean": body,
        }
    )


def test_mentions_inserted_backdated_with_provenance(fresh_db, monkeypatch):
    _mail("m1", "tom", "2018-03-05T10:00:00Z", "We should read The Power Broker sometime.")
    monkeypatch.setattr(
        miner.oliver,
        "complete",
        lambda *a, **k: json.dumps(
            [
                {
                    "title": "The Power Broker",
                    "author": "Robert Caro",
                    "reason": "nominated as a power-and-institutions pick",
                    "reason_kind": "nomination",
                    "message_id": "m1",
                }
            ]
        ),
    )
    res = miner._mine_member_year("tom", 2018, until="2026-01-01", dry_run=False)
    assert res == {"mentions": 1, "messages": 1}
    row = db.recent_book_cloud()[0]
    assert row["mentioned_by"] == "tom" and row["surface"] == "mailing_list"
    assert row["created_at"].startswith("2018-03-05")  # backdated to the real sent date
    agg = db.book_cloud_titles()[0]
    assert agg["first_mentioned"].startswith("2018-03-05")


def test_read_titles_skipped_in_code(fresh_db, monkeypatch):
    _mail("m1", "tom", "2018-03-05T10:00:00Z", "Watchmen was great, and try Piranesi.")
    monkeypatch.setattr(
        miner.oliver,
        "complete",
        lambda *a, **k: json.dumps(
            [
                {"title": "Watchmen", "reason": "praised a past read", "message_id": "m1"},
                {"title": "Piranesi", "reason": "recommended as light fiction", "message_id": "m1"},
            ]
        ),
    )
    res = miner._mine_member_year("tom", 2018, until="2026-01-01", dry_run=False)
    assert res["mentions"] == 1  # read-list title dropped even if
    assert db.recent_book_cloud()[0]["title"] == "Piranesi"  # the model ignores the rule


def test_unparseable_after_retry_returns_none(fresh_db, monkeypatch):
    _mail("m1", "tom", "2018-03-05T10:00:00Z", "hello")
    calls = []
    monkeypatch.setattr(
        miner.oliver, "complete", lambda *a, **k: calls.append(1) or "I can't decide."
    )
    assert miner._mine_member_year("tom", 2018, until="2026-01-01", dry_run=False) is None
    assert len(calls) == 2  # retried once
    assert db.recent_book_cloud() == []


def test_empty_year_costs_nothing(fresh_db, monkeypatch):
    called = []
    monkeypatch.setattr(miner.oliver, "complete", lambda *a, **k: called.append(1) or "[]")
    assert miner._mine_member_year("tom", 2019, until="2026-01-01", dry_run=False) == {
        "mentions": 0,
        "messages": 0,
    }
    assert called == []  # no mail → no model call


def test_dry_run_writes_nothing(fresh_db, monkeypatch, capsys):
    _mail("m1", "tom", "2018-03-05T10:00:00Z", "Try Piranesi.")
    monkeypatch.setattr(
        miner.oliver,
        "complete",
        lambda *a, **k: json.dumps(
            [{"title": "Piranesi", "reason": "recommended as light fiction", "message_id": "m1"}]
        ),
    )
    miner._mine_member_year("tom", 2018, until="2026-01-01", dry_run=True)
    assert db.recent_book_cloud() == []
    assert "Piranesi" in capsys.readouterr().out


def test_until_boundary_respected(fresh_db, monkeypatch):
    _mail("late", "tom", "2026-06-15T00:00:00Z", "Try Piranesi.")
    called = []
    monkeypatch.setattr(miner.oliver, "complete", lambda *a, **k: called.append(1) or "[]")
    res = miner._mine_member_year("tom", 2026, until="2026-06-01T00:00:00Z", dry_run=False)
    assert res == {"mentions": 0, "messages": 0} and called == []
