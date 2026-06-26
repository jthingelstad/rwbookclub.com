from __future__ import annotations

import mailbox
from email.message import EmailMessage

from agent.mail import mail_archive
from agent.mail.email_jmap import InboundEmail


def _message(*, sender: str = "Jamie <jamie@example.test>",
             subject: str = "Re: [rwbookclub] Book picks",
             body: str = "I nominate Cities.\n\n-- \nYou received this message because you are subscribed to the Google Groups") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "R/W Book Club <rwbookclub@googlegroups.com>"
    msg["Date"] = "Thu, 25 Jun 2026 12:00:00 -0500"
    msg["Subject"] = subject
    msg["Message-ID"] = "<m1@example.test>"
    msg["X-GM-THRID"] = "12345"
    msg["X-BeenThere"] = "rwbookclub@googlegroups.com"
    msg.set_content(body)
    return msg


def test_clean_body_removes_google_footer_and_quotes():
    body = (
        "I can make Tuesday.\n\n"
        "On Thu, Jamie wrote:\n"
        "> quoted old text\n"
        "-- \n"
        "You received this message because you are subscribed to the Google Groups"
    )
    assert mail_archive.clean_body(body) == "I can make Tuesday."


def test_normalized_from_mbox_message_resolves_alias_and_thread(fresh_db):
    db = fresh_db
    db.link_member_email("jamie@example.test", "jamie")
    normalized, stats = mail_archive.normalized_from_mbox_message(
        _message(), source_ref="fixture:1",
    )
    assert normalized["message_id"] == "<m1@example.test>"
    assert normalized["thread_id"] == "x-gm-thrid:12345"
    assert normalized["list_id"] == "rwbookclub@googlegroups.com"
    assert normalized["member_slug"] == "jamie"
    assert normalized["subject_normalized"] == "book picks"
    assert normalized["body_clean"] == "I nominate Cities."
    assert not stats["missing_message_id"]


def test_import_mbox_write_seeds_archive_aliases(tmp_path, fresh_db):
    path = tmp_path / "topics.mbox"
    box = mailbox.mbox(path)
    msg = _message(sender="Loren Terveen <terveen@cs.umn.edu>")
    msg.replace_header("Message-ID", "<loren@example.test>")
    box.add(msg)
    box.flush()
    box.close()

    report = mail_archive.import_mbox(path, write=True)
    assert report.total == 1
    assert report.inserted == 1
    assert fresh_db.member_slug_for_email("terveen@cs.umn.edu") == "loren"
    rows = fresh_db.search_mail_archive("nominate", member_slug="loren")
    assert len(rows) == 1


def test_archive_inbound_email_uses_live_jmap_source(fresh_db):
    db = fresh_db
    db.link_member_email("jamie@example.test", "jamie")
    msg = InboundEmail(
        id="jmap1",
        thread_id="thread1",
        message_id="<live@example.test>",
        from_name="Jamie",
        from_email="jamie@example.test",
        to=["rwbookclub@googlegroups.com"],
        cc=[],
        reply_to=[],
        subject="Oliver has email",
        text="Oliver, please remember this.\n\n> old quote",
        received_at="2026-06-25T17:00:00Z",
        references=["<prior@example.test>"],
    )
    assert mail_archive.archive_inbound_email(msg, is_mailing_list=True, member_slug="jamie")
    counts = db.mail_archive_counts()
    assert counts["messages"] == 1
    rows = db.search_mail_archive("remember", member_slug="jamie")
    assert rows[0]["message_id"] == "<live@example.test>"
    thread = db.get_mail_thread("jmap:thread1")
    assert thread["messages"][0]["body_clean"] == "Oliver, please remember this."


def test_archive_inbound_email_resolves_google_groups_display_name(fresh_db):
    db = fresh_db
    msg = InboundEmail(
        id="jmap2",
        thread_id="thread2",
        message_id="<list@example.test>",
        from_name="'Jamie Thingelstad' via rwbookclub",
        from_email="rwbookclub@googlegroups.com",
        to=["rwbookclub@googlegroups.com"],
        cc=[],
        reply_to=[],
        subject="Meeting in 5 days!",
        text="Meeting reminder.",
        received_at="2026-06-25T18:00:00Z",
        references=[],
    )
    assert mail_archive.archive_inbound_email(msg, is_mailing_list=True)
    rows = db.search_mail_archive("Meeting reminder", member_slug="jamie")
    assert rows[0]["from_email"] == "rwbookclub@googlegroups.com"
