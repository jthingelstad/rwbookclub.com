"""Email safety policy for Oliver's inbox and outbound tool."""

from __future__ import annotations

from agent import config, email_policy
from agent.email_jmap import InboundEmail


def msg(
    *,
    from_email: str,
    subject: str = "Hello",
    text: str = "Hello Oliver",
    to: list[str] | None = None,
    cc: list[str] | None = None,
    reply_to: list[str] | None = None,
) -> InboundEmail:
    return InboundEmail(
        id="m1",
        thread_id="t1",
        message_id="msg1@example.test",
        from_name=None,
        from_email=from_email,
        to=to or ["oliver@rwbookclub.com"],
        cc=cc or [],
        reply_to=reply_to or [],
        subject=subject,
        text=text,
        received_at="2026-06-25T13:00:00Z",
        references=[],
    )


class TestInboundEmailPolicy:
    def test_unknown_sender_is_ignored(self):
        decision = email_policy.inbound_decision(
            msg(from_email="noreply@groups.google.com", subject="Invitation to join rwbookclub")
        )
        assert decision.allowed is False
        assert decision.reason == "sender_not_allowed"

    def test_known_member_direct_email_is_allowed(self, fresh_db):
        fresh_db.link_member_email("jamie@thingelstad.com", "jamie")
        decision = email_policy.inbound_decision(msg(from_email="Jamie@Thingelstad.com"))
        assert decision.allowed is True
        assert decision.reason == "known_member"
        assert decision.member_slug == "jamie"
        assert decision.reply_to == ["jamie@thingelstad.com"]

    def test_passive_mailing_list_message_is_ignored(self, fresh_db):
        fresh_db.link_member_email("jamie@thingelstad.com", "jamie")
        decision = email_policy.inbound_decision(
            msg(
                from_email="jamie@thingelstad.com",
                to=[config.BOOK_CLUB_MAILING_LIST_ADDRESS],
                subject="Current book",
                text="I liked chapter 2.",
            )
        )
        assert decision.allowed is False
        assert decision.reason == "mailing_list_not_addressed"
        assert decision.member_slug == "jamie"

    def test_mailing_list_question_replies_to_list(self, fresh_db):
        fresh_db.link_member_email("jamie@thingelstad.com", "jamie")
        decision = email_policy.inbound_decision(
            msg(
                from_email="jamie@thingelstad.com",
                to=[config.BOOK_CLUB_MAILING_LIST_ADDRESS],
                subject="Past pick",
                text="What did we read about North Korea?",
            )
        )
        assert decision.allowed is True
        assert decision.reason == "mailing_list_addressed"
        assert decision.is_mailing_list is True
        assert decision.reply_to == [config.BOOK_CLUB_MAILING_LIST_ADDRESS]

    def test_mailing_list_direct_reference_replies_to_list(self):
        decision = email_policy.inbound_decision(
            msg(
                from_email="member@example.test",
                to=[config.BOOK_CLUB_MAILING_LIST_ADDRESS],
                subject="Oliver",
                text="Oliver, remind us who picked this book.",
            )
        )
        assert decision.allowed is True
        assert decision.reply_to == [config.BOOK_CLUB_MAILING_LIST_ADDRESS]

    def test_quoted_question_does_not_trigger_list_reply(self):
        decision = email_policy.inbound_decision(
            msg(
                from_email="member@example.test",
                to=[config.BOOK_CLUB_MAILING_LIST_ADDRESS],
                subject="Re: Current book",
                text="\n\nOn Jun 25 Jamie wrote:\n> What did we read about North Korea?",
            )
        )
        assert decision.allowed is False
        assert decision.reason == "mailing_list_not_addressed"


class TestOutboundEmailPolicy:
    def test_model_send_allows_linked_member(self, fresh_db):
        fresh_db.link_member_email("jamie@thingelstad.com", "jamie")
        assert email_policy.validate_model_email_recipients(to=["Jamie <jamie@thingelstad.com>"]) is None

    def test_model_send_blocks_unknown_address(self, fresh_db):
        fresh_db.link_member_email("jamie@thingelstad.com", "jamie")
        assert (
            email_policy.validate_model_email_recipients(to=["outsider@example.test"])
            == "Oliver can only email linked book club member addresses from this tool"
        )

    def test_model_send_blocks_mailing_list(self, fresh_db):
        assert email_policy.validate_model_email_recipients(
            to=[config.BOOK_CLUB_MAILING_LIST_ADDRESS]
        ) == (
            "the book club mailing list can only be emailed by approved meeting-cadence paths, "
            "not the general send_email tool"
        )
