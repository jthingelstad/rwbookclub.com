"""The archive miner (Phase 2): extraction → review file → approved load into the timeline.

The LLM call (oliver.complete) is monkeypatched to a fixed reply, so these run offline and
deterministically. They exercise the parse/validate/dedup logic, the review-gate (nothing inserts
until approve:true), provenance-based idempotency, and resumability.
"""

from __future__ import annotations

import json


def _seed_thread(db, thread_id, subject, messages):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO mail_threads (thread_id, subject_normalized, first_sent_at, last_sent_at, "
            "message_count) VALUES (?,?,?,?,?)",
            (thread_id, subject, messages[0][1], messages[-1][1], len(messages)))
        for i, (who, when, body) in enumerate(messages):
            conn.execute(
                "INSERT INTO mail_messages (message_id, thread_id, source, from_name, subject, "
                "sent_at, body_clean) VALUES (?,?,?,?,?,?,?)",
                (f"{thread_id}-{i}", thread_id, "test", who, subject, when, body))


_REPLY = json.dumps([
    {"category": "selection", "kind": "book_picked", "occurred_at": "2018-04-10",
     "member_slugs": ["jamie"], "summary": "Jamie picked Sapiens for the April meeting."},
    {"category": "social", "kind": "dinner", "occurred_at": "2018-04-25",
     "member_slugs": [], "summary": "The club met for dinner before the meeting."},
    {"category": "bogus", "kind": "not_a_real_kind", "occurred_at": "2018-04-10",
     "member_slugs": ["jamie"], "summary": "should be dropped — unknown kind."},
])


def test_parse_events_strips_fences():
    from agent.script import mine_archive_events as m
    fenced = "```json\n[{\"kind\": \"dinner\"}]\n```"
    assert m._parse_events(fenced) == [{"kind": "dinner"}]
    assert m._parse_events("no json here") == []


def test_mine_writes_candidates_and_skips_unknown_kinds(tmp_path, fresh_db, monkeypatch):
    from agent import oliver
    from agent.script import mine_archive_events as m

    _seed_thread(fresh_db, "t1", "April pick", [("Jamie", "2018-04-01 09:00:00", "I pick Sapiens")])
    monkeypatch.setattr(oliver, "complete", lambda *a, **k: _REPLY)
    out = tmp_path / "mined.jsonl"

    stats = m.mine(limit=None, force=False, thread_id=None, out_path=out, model="x", effort="low")
    assert stats["threads"] == 1 and stats["events"] == 2  # bogus kind dropped

    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert {r["kind"] for r in rows} == {"book_picked", "dinner"}
    assert all(r["approve"] is False for r in rows)              # review gate: nothing pre-approved
    assert rows[0]["source"] == "mail:t1#0" and rows[1]["source"] == "mail:t1#1"
    assert rows[0]["member_slugs"] == ["jamie"]
    # progress marker written so a re-run skips this thread
    assert (out.with_suffix(out.suffix + ".threads")).read_text().strip() == "t1"


def test_mine_is_resumable(tmp_path, fresh_db, monkeypatch):
    from agent import oliver
    from agent.script import mine_archive_events as m

    _seed_thread(fresh_db, "t1", "One", [("Jamie", "2018-04-01 09:00:00", "x")])
    calls = []
    monkeypatch.setattr(oliver, "complete", lambda *a, **k: calls.append(1) or "[]")
    out = tmp_path / "mined.jsonl"
    m.mine(limit=None, force=False, thread_id=None, out_path=out, model="x", effort="low")
    m.mine(limit=None, force=False, thread_id=None, out_path=out, model="x", effort="low")
    assert len(calls) == 1  # second run skips the already-done thread


def test_load_only_inserts_approved_and_is_idempotent(tmp_path, fresh_db):
    from agent import clubdb
    from agent.script import mine_archive_events as m

    jamie = clubdb.lookup_member_id("jamie")
    out = tmp_path / "mined.jsonl"
    lines = [
        {"approve": True, "source": "mail:t1#0", "thread_id": "t1", "category": "selection",
         "kind": "book_picked", "occurred_at": "2018-04-10", "member_slugs": ["jamie"],
         "summary": "Jamie picked Sapiens."},
        {"approve": False, "source": "mail:t1#1", "thread_id": "t1", "category": "social",
         "kind": "dinner", "occurred_at": "2018-04-25", "member_slugs": [],
         "summary": "Club dinner."},
    ]
    out.write_text("\n".join(json.dumps(x) for x in lines))

    stats = m.load(out_path=out)
    assert stats == {"inserted": 1, "skipped": 0, "unapproved": 1, "invalid": 0}

    rows = fresh_db.timeline(category="selection")
    assert len(rows) == 1
    ev = rows[0]
    assert ev["kind"] == "book_picked" and ev["member_id"] == jamie
    assert ev["source"] == "mail:t1#0" and ev["actor"] == "oliver"
    assert json.loads(ev["detail"])["summary"] == "Jamie picked Sapiens."
    # the rejected (approve:false) dinner is not on the timeline
    assert fresh_db.timeline(category="social") == []

    # re-load is idempotent — the approved row is already present (dedup on source).
    again = m.load(out_path=out)
    assert again["inserted"] == 0 and again["skipped"] == 1
    assert len(fresh_db.timeline(category="selection")) == 1


def test_load_multi_member_event_is_group_scoped(tmp_path, fresh_db):
    from agent.script import mine_archive_events as m
    out = tmp_path / "mined.jsonl"
    out.write_text(json.dumps({
        "approve": True, "source": "mail:t2#0", "thread_id": "t2", "category": "social",
        "kind": "dinner", "occurred_at": "2019-05-01", "member_slugs": ["jamie", "tom"],
        "summary": "Jamie and Tom hosted dinner."}))
    m.load(out_path=out)
    ev = fresh_db.timeline(category="social")[0]
    assert ev["member_id"] is None  # 2+ members → club/group-scoped row
    assert json.loads(ev["detail"])["members"] == ["jamie", "tom"]
