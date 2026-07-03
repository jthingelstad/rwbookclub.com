"""Cross-medium memory: member-tagged conversations, cross-medium recall, proactive priming,
and two-sided mail archiving — so Oliver doesn't lose the thread between email and Discord.

Reproduces the reported bug: a member emails Oliver about book picks, then in Discord asks to pick
that thread back up. Oliver must recall the email exchange (both sides), labeled 'email'.
"""

from __future__ import annotations

from agent import db, oliver
from agent.mail import mail_archive
from agent.mail.email_jmap import InboundEmail


def test_conversation_medium_labels():
    assert db.conversation_medium("email:list:t1") == "mailing list"
    assert db.conversation_medium("email:thread-abc") == "email"
    assert db.conversation_medium("1234567890") == "Discord"


def test_log_message_tags_member_and_search_filters(fresh_db):
    db.log_message("email:t1", "user", "Which sci-fi should we read?",
                   speaker="Jamie", member_slug="jamie")
    db.log_message("email:t1", "assistant", "Try Blindsight and Nexus.", member_slug="jamie")
    db.log_message("123", "user", "unrelated Tom thing", speaker="Tom", member_slug="tom")
    hits = db.search_conversations("Nexus", member_slug="jamie")
    assert len(hits) == 1 and hits[0]["member_slug"] == "jamie"
    assert hits[0]["role"] == "assistant"           # Oliver's OWN reply is recallable
    assert db.search_conversations("thing", member_slug="jamie") == []  # scoped to the person


def test_recent_threads_for_member_surfaces_other_medium(fresh_db):
    db.log_message("email:t1", "user", "book ideas please", speaker="Jamie", member_slug="jamie")
    db.log_message("email:t1", "assistant",
                   "Superintelligence, The Coming Wave, The Dawn of Everything", member_slug="jamie")
    # Answering in a Discord channel (id 999): the email thread surfaces, labeled 'email'.
    recent = db.recent_threads_for_member("jamie", exclude_channel="999")
    assert len(recent) == 1 and recent[0]["medium"] == "email"
    assert "Coming Wave" in recent[0]["snippet"]
    # The current channel is excluded (Oliver already sees it).
    assert db.recent_threads_for_member("jamie", exclude_channel="email:t1") == []


def test_question_block_primes_cross_medium(fresh_db):
    db.log_message("email:t1", "user", "book ideas", speaker="Jamie", member_slug="jamie")
    db.log_message("email:t1", "assistant", "Blindsight and Nexus", member_slug="jamie")
    block = oliver._question_block("what were those books?", "Jamie", "jamie", None, channel_id="999")
    assert "Recently with them elsewhere" in block and "email" in block
    assert "(today)" in block                                  # thread age surfaces for staleness rules


def test_question_block_priming_shows_stale_age(fresh_db):
    db.log_message("email:t1", "user", "book ideas", speaker="Jamie", member_slug="jamie")
    with db.connect() as c:  # backdate the thread 5 days
        c.execute("UPDATE conversations SET created_at = datetime('now', '-5 days')")
    block = oliver._question_block("hi", "Jamie", "jamie", None, channel_id="999")
    assert "(5 days ago)" in block


def test_age_text_edges():
    assert oliver._age_text(None) == "some time ago"
    assert oliver._age_text("not-a-date") == "some time ago"
    assert oliver._age_text("2020-01-01 00:00:00").endswith("days ago")
    # Unrecognized speaker → no member → no priming (and no per-answer lookup cost).
    plain = oliver._question_block("hi", "Randomvisitor", None, None, channel_id="999")
    assert "Recently with them elsewhere" not in plain


def _inbound(thread_id: str, from_email: str, subject: str, message_id: str) -> InboundEmail:
    return InboundEmail(
        id="in1", thread_id=thread_id, message_id=message_id, from_name="Jamie",
        from_email=from_email, to=["oliver@rwbookclub.com"], cc=[], reply_to=[],
        subject=subject, text="what should we read?",
        received_at="2026-06-30T10:00:00Z", references=[],
    )


def test_outbound_email_archived_two_sided(fresh_db):
    inbound = _inbound("thr-1", "jthingelstad@gmail.com", "Book ideas?", "<in@x>")
    mail_archive.archive_inbound_email(inbound, is_mailing_list=False, member_slug="jamie")
    mail_archive.archive_outbound_email(
        inbound, body="Try Blindsight and Nexus.", to_emails=["jthingelstad@gmail.com"],
        subject="Re: Book ideas?", member_slug="jamie", is_mailing_list=False, sent_email_id="e1")
    # Oliver's own reply is now searchable and member-linked...
    found = db.search_mail_archive("Blindsight", member_slug="jamie")
    assert found, "Oliver's own reply is in the archive"
    # ...and filed under the SAME thread as the inbound, so the thread reads two-sided.
    thread = db.get_mail_thread(found[0]["thread_id"])
    froms = [m["from_email"] for m in thread["messages"]]
    assert "jthingelstad@gmail.com" in froms                        # member's side
    assert any("oliver" in (f or "") for f in froms)                # Oliver's side


def test_backfill_resolves_speaker_names(fresh_db):
    from agent.script.archive.backfill_conversation_members import backfill
    db.log_message("email:t9", "user", "hello there", speaker="Jamie Thingelstad")  # full display name
    db.log_message("email:t9", "assistant", "hi Jamie")                             # inherits
    db.log_message("777", "user", "yo", speaker="Erik")
    res = backfill()
    assert res["tagged"] >= 3
    assert db.search_conversations("hello", member_slug="jamie")     # user turn resolved
    assert db.search_conversations("hi Jamie", member_slug="jamie")  # assistant inherited jamie
    # Idempotent: a second run tags nothing new.
    assert backfill()["tagged"] == 0
