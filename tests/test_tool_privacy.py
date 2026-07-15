"""Code-enforced privacy boundaries for Oliver's model-callable tools."""

from __future__ import annotations

import json

from agent import clubdb, config, db, identities
from agent.club import meeting_rules
from agent.tools import dispatch

JAMIE_CTX = {"speaker": "Jamie", "speaker_user_id": "u1", "member_slug": "jamie"}
ADMIN_CTX = {
    "speaker": "Jamie",
    "speaker_user_id": str(config.ADMIN_USER_ID),
    "member_slug": "jamie",
}


def _call(name: str, tool_input: dict, ctx: dict) -> object:
    return json.loads(dispatch(name, tool_input, ctx))


def _mail(message_id: str, thread_id: str, member: str, body: str, *,
          list_id: str | None = None) -> None:
    db.upsert_mail_message({
        "message_id": message_id,
        "thread_id": thread_id,
        "source": "test",
        "list_id": list_id,
        "from_email": f"{member}@example.test",
        "from_name": member.title(),
        "member_slug": member,
        "subject": "Lighthouse candidates",
        "sent_at": f"2026-06-0{message_id[-1]}T12:00:00Z",
        "received_at": f"2026-06-0{message_id[-1]}T12:00:00Z",
        "body_text": body,
        "body_clean": body,
    })


def test_private_tools_require_linked_identity_but_public_corpus_does_not(fresh_db):
    denied = _call("recall", {}, {})
    assert denied == {"error": "this tool requires a linked club-member identity"}
    assert isinstance(_call("club_stats", {}, {}), dict)


def test_admin_authority_follows_the_linked_member_across_email(fresh_db):
    identities.link_member_identity(str(config.ADMIN_USER_ID), "jamie")
    email_ctx = {
        "speaker": "Jamie",
        "speaker_user_id": "email:jamie@example.test",
        "member_slug": "jamie",
    }
    assert isinstance(_call("meeting_campaign", {}, email_ctx), dict)


def test_memory_recall_is_self_plus_club_and_admin_can_audit(fresh_db):
    db.add_memory("Jamie private lighthouse note", scope="member", subject="jamie")
    db.add_memory("Nick private lighthouse note", scope="member", subject="nick")
    db.add_memory("Shared lighthouse lore", scope="club")
    db.add_memory("Internal lighthouse note", scope="general")

    visible = _call("recall", {"query": "lighthouse"}, JAMIE_CTX)
    notes = {row["note"] for row in visible}
    assert notes == {"Jamie private lighthouse note", "Shared lighthouse lore"}

    denied = _call("recall", {"subject": "nick"}, JAMIE_CTX)
    assert denied == {"error": "another member's private memories are unavailable"}
    denied_write = _call(
        "remember",
        {"note": "Cross-member write", "scope": "member", "subject": "nick"},
        JAMIE_CTX,
    )
    assert denied_write == {"error": "you can only save member-private notes about yourself"}

    admin_rows = _call("recall", {"subject": "nick"}, ADMIN_CTX)
    assert [row["note"] for row in admin_rows] == ["Nick private lighthouse note"]


def test_discussion_search_shares_club_channels_but_isolates_direct_email(fresh_db):
    db.log_message("123", "user", "lighthouse shared discord", speaker="Nick", member_slug="nick")
    db.log_message(
        "email:list:club", "user", "lighthouse shared mailing list",
        speaker="Nick", member_slug="nick",
    )
    db.log_message(
        "email:jamie-private", "user", "lighthouse Jamie private",
        speaker="Jamie", member_slug="jamie",
    )
    db.log_message(
        "email:nick-private", "user", "lighthouse Nick private",
        speaker="Nick", member_slug="nick",
    )

    visible = _call("search_discussion", {"query": "lighthouse"}, JAMIE_CTX)
    content = {row["content"] for row in visible}
    assert content == {
        "lighthouse shared discord",
        "lighthouse shared mailing list",
        "lighthouse Jamie private",
    }
    assert _call("search_discussion", {"query": "lighthouse", "member": "nick"}, JAMIE_CTX) == {
        "error": "another member's private conversation history is unavailable",
    }
    admin_content = {
        row["content"] for row in _call(
            "search_discussion", {"query": "lighthouse"}, ADMIN_CTX)
    }
    assert admin_content == content | {"lighthouse Nick private"}


def test_mail_search_and_thread_reads_are_row_scoped_and_pii_minimized(fresh_db):
    _mail("m1", "shared-thread", "nick", "lighthouse shared list message",
          list_id="rwbookclub@googlegroups.com")
    _mail("m2", "jamie-thread", "jamie", "lighthouse Jamie direct message")
    _mail("m3", "nick-thread", "nick", "lighthouse Nick direct message")
    db.rebuild_mail_thread_stats()

    visible = _call("search_mail_archive", {"query": "lighthouse"}, JAMIE_CTX)
    assert {row["thread_id"] for row in visible} == {"shared-thread", "jamie-thread"}
    assert all("from_email" not in row for row in visible)

    own_thread = _call("get_mail_thread", {"thread_id": "jamie-thread"}, JAMIE_CTX)
    assert own_thread["messages"][0]["body_clean"] == "lighthouse Jamie direct message"
    assert "from_email" not in own_thread["messages"][0]
    assert "participants_json" not in own_thread["thread"]
    assert _call("get_mail_thread", {"thread_id": "nick-thread"}, JAMIE_CTX) == {
        "error": "no accessible mail thread",
    }

    admin_rows = _call("search_mail_archive", {"query": "lighthouse"}, ADMIN_CTX)
    assert {row["thread_id"] for row in admin_rows} == {
        "shared-thread", "jamie-thread", "nick-thread",
    }
    assert all("from_email" not in row for row in admin_rows)
    assert _call("get_mail_thread", {"thread_id": "nick-thread"}, ADMIN_CTX)["messages"]


def test_book_cloud_keeps_direct_email_mentions_private(fresh_db):
    db.add_book_cloud_entry(
        title="Shared Book", reason="lighthouse shared", surface="discord", mentioned_by="nick")
    db.add_book_cloud_entry(
        title="Jamie's Book", reason="lighthouse own", surface="email", mentioned_by="jamie")
    db.add_book_cloud_entry(
        title="Nick's Book", reason="lighthouse other", surface="email", mentioned_by="nick")

    member_rows = _call("book_cloud_recent", {}, JAMIE_CTX)
    assert {row["title"] for row in member_rows} == {"Shared Book", "Jamie's Book"}
    admin_rows = _call("book_cloud_recent", {}, ADMIN_CTX)
    assert {row["title"] for row in admin_rows} == {
        "Shared Book", "Jamie's Book", "Nick's Book",
    }


def test_pick_prospects_cannot_select_another_private_profile(fresh_db):
    denied = _call("pick_prospects", {"member": "nick"}, JAMIE_CTX)
    assert denied == {"error": "private pick guidance can only use your own member profile"}


def test_meeting_tools_expose_only_own_member_signals_outside_admin(fresh_db):
    meeting = meeting_rules.next_meeting()
    jamie_id = clubdb.lookup_member_id("jamie")
    nick_id = clubdb.lookup_member_id("nick")
    db.record_attendance_report(meeting["meetingId"], jamie_id, "yes")
    db.record_reading_report(meeting["meetingId"], jamie_id, "on_track", progress="halfway")
    db.record_attendance_report(meeting["meetingId"], nick_id, "no")
    db.record_reading_report(meeting["meetingId"], nick_id, "behind", progress="chapter 2")

    status = _call("current_meeting_status", {}, JAMIE_CTX)
    assert [row["memberSlug"] for row in status["attendance"]] == ["jamie"]
    assert "pickerAvailable" not in status
    assert status["counts"]["yes"] == 1 and status["counts"]["no"] == 1

    reading = _call("reading_status", {}, JAMIE_CTX)
    assert [row["memberSlug"] for row in reading["statuses"]] == ["jamie"]
    readiness = _call("meeting_readiness", {}, JAMIE_CTX)
    assert [row["memberSlug"] for row in readiness["members"]] == ["jamie"]
    assert readiness["recommendedActions"] == []

    club_state = _call("current_club_state", {}, JAMIE_CTX)
    assert "feedback" not in club_state
    assert all("discordLinked" not in member for member in club_state["members"])
    assert [
        row["memberSlug"] for row in club_state["nextMeeting"]["attendance"]
    ] == ["jamie"]

    assert _call("meeting_campaign", {}, JAMIE_CTX) == {
        "error": "this tool is available only to the club admin",
    }
    admin_campaign = _call("meeting_campaign", {}, ADMIN_CTX)
    assert {row["memberSlug"] for row in admin_campaign["members"]} >= {"jamie", "nick"}
