"""The slash-command surface after the 2026-07 command review.

Webapp-superseded commands are retired (contact link-email/link-sms/list, the memory group,
library add-book); link-member moved under admin; webapp→my-club and stats→status renamed.
This pins the tree so a refactor can't silently resurrect or drop a command.
"""

from agent import commands


def _names(group) -> set[str]:
    return {c.name for c in group.commands}


def test_top_level_groups_and_commands():
    top = _names(commands.oliver_cmds)
    assert {"ping", "my-club", "whoami", "reading", "meeting", "timeline", "admin"} <= top
    # retired groups + old command names must not come back
    assert {"contact", "memory", "library", "webapp"}.isdisjoint(top)


def test_admin_group_contents():
    admin = next(c for c in commands.oliver_cmds.commands if c.name == "admin")
    names = _names(admin)
    assert {"status", "link-member", "release-notes", "postscript", "feedback",
            "proposals", "resolve", "reattribute-mail", "tick"} <= names
    assert "stats" not in names  # renamed to status


def test_meeting_and_timeline_keep_their_commands():
    meeting = next(c for c in commands.oliver_cmds.commands if c.name == "meeting")
    assert {"check-in", "roll-call", "dashboard"} <= _names(meeting)
    timeline = next(c for c in commands.oliver_cmds.commands if c.name == "timeline")
    assert {"log", "show"} <= _names(timeline)
