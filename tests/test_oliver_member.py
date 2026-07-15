"""Oliver's own member record (the sixth member) and the human-mechanics boundary.

Oliver is a real club_members row — public profile, webapp login — but human-only club
machinery (roll calls, check-ins, outreach, audits, taste lenses, the prompt roster) must
never target him: it enumerates members via corpus_read.human_current_members() or filters
config.OLIVER_MEMBER_SLUG explicitly.
"""

import pytest

from agent import access, config, clubdb, db
from agent import corpus_read as cr
from agent.club import meeting_rules
from agent.tool_handlers import picking
from agent.tool_handlers.context import RequestContext

pytestmark = pytest.mark.usefixtures("fresh_db")


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
    monkeypatch.setattr(picking.cr, "members", _fake_members)
    monkeypatch.setattr(picking.cr, "member_history", lambda slug: {})
    request = RequestContext(
        actor=access.Actor(member_slug=None, is_admin=False),
        channel_id=None,
        speaker=None,
        speaker_user_id=None,
        source_message_id=None,
    )
    lenses = picking.member_lenses(request)
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
