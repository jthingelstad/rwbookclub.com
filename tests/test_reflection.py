"""Weekly reflective memory: gathering/grouping, provenance-enforced apply, parse safety,
watermark advance, and quiet no-ops."""

from __future__ import annotations

import json

from agent import db, reflection


def _log_turns(slug: str, n: int, channel: str = "999"):
    for i in range(n):
        db.log_message(
            channel, "user", f"turn {i} about books", speaker=slug.title(), member_slug=slug
        )


def test_quiet_week_no_llm_call_and_watermark_advances(fresh_db, monkeypatch):
    called = []
    monkeypatch.setattr(reflection.oliver, "complete", lambda *a, **k: called.append(1) or "{}")
    db.log_message("999", "user", "anon passer-by", speaker="Visitor")  # no member tag
    out = reflection.run()
    assert out == {"members": 0}
    assert called == []  # no model call on a quiet week
    state = db.get_job_state(reflection.JOB_KEY)
    assert state and state["conv_id"] >= 1  # cursor still advanced past the turn


def test_min_turns_threshold_skips_trivial(fresh_db, monkeypatch):
    called = []
    monkeypatch.setattr(reflection.oliver, "complete", lambda *a, **k: called.append(1) or "{}")
    _log_turns("jamie", 2)  # below MIN_TURNS, no mail
    assert reflection.run()["members"] == 0 and called == []


def test_adds_land_with_reflection_source(fresh_db, monkeypatch):
    _log_turns("jamie", 4)
    monkeypatch.setattr(
        reflection.oliver,
        "complete",
        lambda *a, **k: json.dumps(
            {
                "add": ["Prefers translated fiction", "DNFs dense specialist slogs"],
                "update": [],
                "retire": [],
            }
        ),
    )
    out = reflection.run()
    assert out["results"]["jamie"] == {"add": 2, "update": 0, "retire": 0}
    mems = db.get_memories(subject="jamie")
    assert {m["note"] for m in mems} >= {
        "Prefers translated fiction",
        "DNFs dense specialist slogs",
    }
    assert all(m["source"] == reflection.SOURCE for m in mems)


def test_provenance_enforced_in_code(fresh_db, monkeypatch):
    # A member-requested memory (source = speaker name) must survive a model that tries to
    # update/retire it; only reflection-owned ids are touchable.
    protected = db.add_memory(
        "Jamie asked me to remember his picks alternate fiction/nonfiction",
        scope="member",
        subject="jamie",
        source="Jamie",
    )
    owned = db.add_memory(
        "Old reflection note", scope="member", subject="jamie", source=reflection.SOURCE
    )
    _log_turns("jamie", 4)
    monkeypatch.setattr(
        reflection.oliver,
        "complete",
        lambda *a, **k: json.dumps(
            {
                "add": [],
                "update": [{"id": protected, "note": "OVERWRITTEN"}],
                "retire": [protected, owned],
            }
        ),
    )
    out = reflection.run()
    assert out["results"]["jamie"] == {"add": 0, "update": 0, "retire": 1}  # only `owned` retired
    notes = {m["id"]: m["note"] for m in db.get_memories(subject="jamie")}
    assert notes[protected].startswith("Jamie asked me")  # untouched
    assert owned not in notes  # reflection's own note retired


def test_unparseable_output_skips_member_and_keeps_watermark(fresh_db, monkeypatch):
    _log_turns("jamie", 4)
    monkeypatch.setattr(reflection.oliver, "complete", lambda *a, **k: "Sorry, I could not decide.")
    out = reflection.run()
    assert out["results"]["jamie"] == {"skipped": "unparseable"}
    assert db.get_memories(subject="jamie") == []  # nothing written
    state = db.get_job_state(reflection.JOB_KEY) or {}
    assert int(state.get("conv_id") or 0) == 0  # watermark NOT advanced → retried next week


def test_mailing_list_material_included_and_grouped(fresh_db, monkeypatch):
    # Seed the mail cursor, then archive a member post newer than it.
    db.set_job_state(reflection.JOB_KEY, {"conv_id": 0, "mail_sent_at": "2026-01-01T00:00:00Z"})
    db.upsert_mail_message(
        {
            "message_id": "m-1",
            "thread_id": "t-1",
            "from_email": "jthingelstad@gmail.com",
            "from_name": "Jamie",
            "member_slug": "jamie",
            "subject": "Picks",
            "list_id": "rwbookclub@googlegroups.com",
            "sent_at": "2026-06-01T00:00:00Z",
            "received_at": "2026-06-01T00:00:00Z",
            "body_text": "I want more translated fiction next year.",
            "body_clean": "I want more translated fiction next year.",
        }
    )
    prompts = {}

    def fake_complete(system, user, **kw):
        prompts["user"] = user
        return json.dumps({"add": ["Wants more translated fiction"], "update": [], "retire": []})

    monkeypatch.setattr(reflection.oliver, "complete", fake_complete)
    out = reflection.run()
    assert out["results"]["jamie"]["add"] == 1  # mail alone is enough (no turn minimum)
    assert "[mailing list]" in prompts["user"]
    state = db.get_job_state(reflection.JOB_KEY)
    assert state["mail_sent_at"] == "2026-06-01T00:00:00Z"  # mail cursor advanced


def test_first_run_mail_cursor_is_forward_only(fresh_db, monkeypatch):
    # Pre-existing archive mail must NOT be reflected on the first run (forward-only cursor).
    db.upsert_mail_message(
        {
            "message_id": "old-1",
            "thread_id": "t-0",
            "from_email": "jthingelstad@gmail.com",
            "from_name": "Jamie",
            "member_slug": "jamie",
            "subject": "Ancient",
            "sent_at": "2020-01-01T00:00:00Z",
            "received_at": "2020-01-01T00:00:00Z",
            "body_text": "old",
            "body_clean": "old",
        }
    )
    called = []
    monkeypatch.setattr(reflection.oliver, "complete", lambda *a, **k: called.append(1) or "{}")
    reflection.run()
    assert called == []  # old mail ignored
    state = db.get_job_state(reflection.JOB_KEY)
    assert state["mail_sent_at"] == "2020-01-01T00:00:00Z"  # cursor seeded at archive max


def test_consolidate_club_scope(fresh_db, monkeypatch):
    monkeypatch.setattr(
        reflection.oliver,
        "complete",
        lambda *a, **k: json.dumps(
            {
                "add": ["The December meeting is traditionally social, no book"],
                "update": [],
                "retire": [],
            }
        ),
    )
    out = reflection.consolidate(
        ["[mailing list] jamie — Dec: let's do the usual social"], scope="club"
    )
    assert out == {"add": 1, "update": 0, "retire": 0}
    club = db.get_memories(scope="club")
    assert club[0]["subject"] is None and club[0]["source"] == reflection.SOURCE
    assert "December meeting" in club[0]["note"]


def test_club_provenance_protected(fresh_db, monkeypatch):
    protected = db.add_memory("Admin-curated club fact", scope="club", source="admin")
    monkeypatch.setattr(
        reflection.oliver,
        "complete",
        lambda *a, **k: json.dumps(
            {"add": [], "update": [{"id": protected, "note": "OVERWRITTEN"}], "retire": [protected]}
        ),
    )
    out = reflection.consolidate(["material"], scope="club")
    assert out == {"add": 0, "update": 0, "retire": 0}  # both ops dropped
    assert db.get_memories(scope="club")[0]["note"] == "Admin-curated club fact"


def test_weekly_run_includes_club_lane(fresh_db, monkeypatch):
    _log_turns("jamie", 4)
    calls = []

    def fake_complete(system, user, **kw):
        calls.append(system)
        if "THE CLUB ITSELF" in system:
            return json.dumps(
                {"add": ["Running joke: GEB is eternally deferred"], "update": [], "retire": []}
            )
        return json.dumps({"add": ["Likes big-idea nonfiction"], "update": [], "retire": []})

    monkeypatch.setattr(reflection.oliver, "complete", fake_complete)
    out = reflection.run()
    assert out["results"]["jamie"]["add"] == 1
    assert out["results"]["club"]["add"] == 1  # club lane ran
    assert len(calls) == 2  # one member call + one club call
    assert db.get_memories(scope="club")[0]["note"].startswith("Running joke")
    with db.connect() as c:
        body = c.execute(
            "SELECT body FROM activity_events WHERE kind='reflection' ORDER BY id DESC LIMIT 1"
        ).fetchone()["body"]
    assert "club: +1" in body  # audit line includes the club lane


def test_private_email_is_excluded_from_club_reflection(fresh_db, monkeypatch):
    _log_turns("jamie", 4, channel="email:private-thread")
    calls = []

    def fake_complete(system, user, **kw):
        calls.append((system, user))
        return json.dumps({"add": ["A durable private preference"], "update": [], "retire": []})

    monkeypatch.setattr(reflection.oliver, "complete", fake_complete)
    out = reflection.run()
    assert out["results"]["jamie"]["add"] == 1
    assert "club" not in out["results"]
    assert len(calls) == 1
    assert "[email] Jamie" in calls[0][1]
    assert db.get_memories(scope="club") == []


def test_dry_run_writes_nothing(fresh_db, monkeypatch, capsys):
    _log_turns("jamie", 4)
    monkeypatch.setattr(
        reflection.oliver,
        "complete",
        lambda *a, **k: json.dumps({"add": ["A durable note"], "update": [], "retire": []}),
    )
    reflection.run(dry_run=True)
    assert db.get_memories(subject="jamie") == []  # no memory written
    assert db.get_job_state(reflection.JOB_KEY) is None  # no watermark written
    assert "+ A durable note" in capsys.readouterr().out  # proposal was printed
