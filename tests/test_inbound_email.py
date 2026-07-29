"""Inbound email orchestration boundaries: archive, reply, and durable dedupe."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from agent import config, db, identities
from agent.mail import inbound
from agent.mail.email_jmap import InboundEmail


def _message() -> InboundEmail:
    return InboundEmail(
        id="inbound-1",
        thread_id="thread-1",
        message_id="message-1@example.test",
        from_name="Jamie",
        from_email="jamie@example.test",
        to=["oliver@rwbookclub.com"],
        cc=[],
        reply_to=[],
        subject="A normal question",
        text="What should I read next?",
        received_at="2026-07-15T12:00:00Z",
        references=[],
    )


def test_post_send_bookkeeping_failure_does_not_duplicate_reply(fresh_db, monkeypatch):
    identities.link_member_email("jamie@example.test", "jamie")
    sent = []
    monkeypatch.setattr(inbound.mail_archive, "archive_inbound_email", lambda *a, **k: True)
    monkeypatch.setattr(
        inbound.mail_archive,
        "archive_outbound_email",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("archive unavailable")),
    )
    monkeypatch.setattr(inbound.oliver, "answer", lambda *a, **k: "Try Blindsight.")
    monkeypatch.setattr(
        inbound.outbound,
        "send",
        lambda **kwargs: sent.append(kwargs) or {"emailId": "sent-1"},
    )
    monkeypatch.setattr(inbound.email_jmap, "mark_seen", lambda *a, **k: None)

    message = _message()
    asyncio.run(inbound.handle(message, schedule_publish=lambda: None))
    asyncio.run(inbound.handle(message, schedule_publish=lambda: None))

    assert len(sent) == 1
    assert db.email_processed(message.id)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status, reply_email_id FROM inbound_emails WHERE email_id=?", (message.id,)
        ).fetchone()
    assert dict(row) == {"status": "processed", "reply_email_id": "sent-1"}


def test_unaddressed_mailing_list_post_is_archived_without_generation_or_send(
    fresh_db, monkeypatch
):
    identities.link_member_email("jamie@example.test", "jamie")
    archived = []
    seen = []
    monkeypatch.setattr(
        inbound.mail_archive,
        "archive_inbound_email",
        lambda *args, **kwargs: archived.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(
        inbound.email_jmap, "mark_seen", lambda *args, **kwargs: seen.append((args, kwargs))
    )
    monkeypatch.setattr(
        inbound.oliver,
        "answer",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model was called")),
    )
    monkeypatch.setattr(
        inbound.outbound,
        "send",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("mail was sent")),
    )
    message = replace(
        _message(),
        to=[config.BOOK_CLUB_MAILING_LIST_ADDRESS],
        reply_to=[config.BOOK_CLUB_MAILING_LIST_ADDRESS],
        subject="Meeting logistics",
        text="We'll meet Tuesday at 6:00. P.S. Oliver now handles member pronouns. ;-)",
    )

    asyncio.run(inbound.handle(message, schedule_publish=lambda: None))

    assert len(archived) == 1
    assert archived[0][1] == {"is_mailing_list": True, "member_slug": "jamie"}
    assert seen == [((message.id,), {})]
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status, error FROM inbound_emails WHERE email_id=?", (message.id,)
        ).fetchone()
    assert dict(row) == {
        "status": "ignored",
        "error": "mailing_list_no_reply:not_explicitly_addressed",
    }
