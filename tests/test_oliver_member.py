"""Oliver's own member record (the sixth member) and the human-mechanics boundary.

Oliver is a real club_members row — public profile, webapp login — but human-only club
machinery (roll calls, check-ins, outreach, audits, taste lenses, the prompt roster) must
never target him: it enumerates members via corpus_read.human_current_members() or filters
config.OLIVER_MEMBER_SLUG explicitly.
"""

from agent import config, clubdb, db
from agent import corpus_read as cr
from agent.club import meeting_rules


def _fake_members():
    return [
        {"slug": "jamie", "name": "Jamie", "isCurrent": True},
        {"slug": "oliver", "name": "Oliver", "isCurrent": True},
        {"slug": "erik", "name": "Erik", "isCurrent": False},
    ]


def test_human_current_members_excludes_oliver_and_former(monkeypatch):
    monkeypatch.setattr(cr, "members", _fake_members)
    assert [m["slug"] for m in cr.human_current_members()] == ["jamie"]


def test_prompt_roster_excludes_oliver(monkeypatch):
    from agent import context
    monkeypatch.setattr(cr, "members", _fake_members)
    roster = context.club_context()
    assert "Oliver (" not in roster  # no "Oliver (0 picks, 0 hosted)" in his own prompt


def test_member_lenses_exclude_oliver(monkeypatch):
    from agent import tools
    monkeypatch.setattr(tools.cr, "members", _fake_members)
    monkeypatch.setattr(tools.cr, "member_history", lambda slug: {})
    monkeypatch.setattr(tools.db, "get_memories", lambda **kw: [])
    lenses = tools._member_lenses()
    assert "oliver" not in lenses and "jamie" in lenses


def test_meeting_status_attendance_excludes_oliver():
    # Real DB path: create the Oliver member row (per-test club reseed keeps this isolated),
    # then confirm attendance rows never include him.
    with db.connect() as conn:
        clubdb.create_member(conn, "Oliver")
    status = meeting_rules.meeting_status()
    slugs = {r["memberSlug"] for r in status["attendance"]}
    assert config.OLIVER_MEMBER_SLUG not in slugs
    assert slugs  # humans are still there
