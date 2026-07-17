"""The autonomous outreach engine: Oliver's per-member REACH/WAIT call + the scheduler dispatch.

`decide_outreach` is Oliver's judgment (stub the underlying LLM call). `_run_meeting_outreach` is the
scheduler glue: for each candidate from outreach_plan it forces the mustReach ones, consults Oliver on
the rest, routes attendance vs reading to the right sender, and lets those senders record the
attendance_request / reading_request events. The plan is stubbed so the test is date-independent.
"""

from __future__ import annotations

import asyncio

from agent import clubdb, identities, oliver
from agent.club import meeting_campaign, meeting_rules, outreach

# ── oliver.decide_outreach ──────────────────────────────────────────────────


def test_decide_outreach_reach(monkeypatch):
    monkeypatch.setattr(oliver, "complete", lambda *a, **k: "REACH")
    assert oliver.decide_outreach({"kind": "attendance", "member": "Tom"}) is True


def test_decide_outreach_wait(monkeypatch):
    monkeypatch.setattr(oliver, "complete", lambda *a, **k: "WAIT — they were just asked")
    assert oliver.decide_outreach({"kind": "reading", "member": "Tom"}) is False


def test_decide_outreach_fails_open_to_reach(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("api down")

    monkeypatch.setattr(oliver, "complete", boom)
    assert oliver.decide_outreach({"kind": "attendance", "member": "Tom"}) is True


def test_decide_outreach_ambiguous_defaults_to_reach(monkeypatch):
    monkeypatch.setattr(oliver, "complete", lambda *a, **k: "hmm, hard to say")
    assert oliver.decide_outreach({"kind": "reading", "member": "Tom"}) is True


# ── _run_meeting_outreach dispatch ──────────────────────────────────────────


def _setup(fresh_db, monkeypatch, *, decide):
    """Seed two reachable members + a fixed plan; stub the LLM + the email send. Returns helpers."""
    db = fresh_db
    identities.link_member_email("jamie@example.test", "jamie")
    identities.link_member_email("tom@example.test", "tom")
    meeting = meeting_rules.next_meeting()
    status = meeting_rules.meeting_status(meeting["meetingId"])
    jamie, tom = clubdb.lookup_member_id("jamie"), clubdb.lookup_member_id("tom")
    plan = [
        {
            "memberSlug": "jamie",
            "memberId": jamie,
            "member": "Jamie",
            "kind": "attendance",
            "mustReach": True,
            "attendance": "pending",
            "reading": "unknown",
            "daysUntilMeeting": 8,
            "daysSinceLastAsk": None,
            "asksSoFar": 0,
            "readingProgress": None,
        },
        {
            "memberSlug": "tom",
            "memberId": tom,
            "member": "Tom",
            "kind": "reading",
            "mustReach": False,
            "attendance": "yes",
            "reading": "behind",
            "daysUntilMeeting": 8,
            "daysSinceLastAsk": 4,
            "asksSoFar": 1,
            "readingProgress": "halfway",
        },
    ]
    consulted = []
    monkeypatch.setattr(meeting_campaign, "snapshot", lambda: {})
    monkeypatch.setattr(meeting_campaign, "outreach_plan", lambda data, *, today=None: plan)
    monkeypatch.setattr(
        oliver, "decide_outreach", lambda c: consulted.append(c["memberSlug"]) or decide
    )
    monkeypatch.setattr(oliver, "compose", lambda *a, **k: "Email body.")  # no real LLM
    sent = []
    monkeypatch.setattr(
        outreach.outbound, "send", lambda **kw: sent.append(kw) or {"emailId": f"e{len(sent)}"}
    )
    return db, meeting, status, jamie, tom, consulted, sent


def test_dispatch_forces_mustreach_and_consults_oliver_for_the_rest(fresh_db, monkeypatch):
    db, meeting, status, jamie, tom, consulted, sent = _setup(fresh_db, monkeypatch, decide=True)
    mid = meeting["meetingId"]

    posted = asyncio.run(outreach.run(meeting, status))

    assert posted == 2
    # Oliver was asked ONLY about the discretionary candidate (tom); jamie was forced (mustReach).
    assert consulted == ["tom"]
    assert sorted(s["to"][0] for s in sent) == ["jamie@example.test", "tom@example.test"]
    # The senders recorded the right events on the timeline.
    assert db.meeting_events(mid, member_id=jamie, kind="attendance_requested")
    assert db.meeting_events(mid, member_id=tom, kind="reading_requested")
    # Email-only: the autonomous path opens no Discord roll call.
    assert db.current_roll_call(mid) is None


def test_dispatch_respects_oliver_waiting(fresh_db, monkeypatch):
    db, meeting, status, jamie, tom, consulted, sent = _setup(fresh_db, monkeypatch, decide=False)
    mid = meeting["meetingId"]

    posted = asyncio.run(outreach.run(meeting, status))

    # Only the forced (mustReach) attendance email goes out; Oliver chose WAIT for tom's reading.
    assert posted == 1
    assert consulted == ["tom"]
    assert [s["to"][0] for s in sent] == ["jamie@example.test"]
    assert db.meeting_events(mid, member_id=jamie, kind="attendance_requested")
    assert db.meeting_events(mid, member_id=tom, kind="reading_requested") == []
