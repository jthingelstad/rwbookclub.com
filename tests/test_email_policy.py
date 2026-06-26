"""Email safety policy for Oliver's inbox and outbound tool."""

from __future__ import annotations

from agent import config
from agent.mail import email_policy
from agent.mail.email_jmap import InboundEmail


def msg(
    *,
    from_email: str,
    from_name: str | None = None,
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
        from_name=from_name,
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
        assert decision.allowed is True
        assert decision.reason == "mailing_list_candidate"
        assert decision.is_mailing_list is True
        assert decision.member_slug == "jamie"

    def test_generic_mailing_list_question_is_model_candidate(self, fresh_db):
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
        assert decision.reason == "mailing_list_candidate"

    def test_direct_oliver_question_is_model_candidate(self, fresh_db):
        fresh_db.link_member_email("jamie@thingelstad.com", "jamie")
        decision = email_policy.inbound_decision(
            msg(
                from_email="jamie@thingelstad.com",
                to=[config.BOOK_CLUB_MAILING_LIST_ADDRESS],
                subject="Past pick",
                text="Oliver, what did we read about North Korea?",
            )
        )
        assert decision.allowed is True
        assert decision.reason == "mailing_list_candidate"
        assert decision.is_mailing_list is True
        assert decision.reply_to == [config.BOOK_CLUB_MAILING_LIST_ADDRESS]

    def test_mailing_list_direct_reference_is_model_candidate(self):
        decision = email_policy.inbound_decision(
            msg(
                from_email="member@example.test",
                to=[config.BOOK_CLUB_MAILING_LIST_ADDRESS],
                subject="Oliver",
                text="Oliver, remind us who picked this book.",
            )
        )
        assert decision.allowed is True
        assert decision.reason == "mailing_list_candidate"
        assert decision.reply_to == [config.BOOK_CLUB_MAILING_LIST_ADDRESS]

    def test_mailing_list_sender_display_name_resolves_member(self):
        decision = email_policy.inbound_decision(
            msg(
                from_email=config.BOOK_CLUB_MAILING_LIST_ADDRESS,
                from_name="'Jamie Thingelstad' via rwbookclub",
                to=[config.BOOK_CLUB_MAILING_LIST_ADDRESS],
                subject="Meeting in 5 days!",
            )
        )
        assert decision.allowed is True
        assert decision.is_mailing_list is True
        assert decision.member_slug == "jamie"

    def test_direct_unknown_sender_cannot_spoof_member_by_display_name(self):
        decision = email_policy.inbound_decision(
            msg(
                from_email="unknown@example.test",
                from_name="Jamie Thingelstad",
                to=["oliver@rwbookclub.com"],
            )
        )
        assert decision.allowed is False
        assert decision.reason == "sender_not_allowed"

    def test_display_name_cleanup_for_google_groups(self):
        assert email_policy.known_member_slug_for_display_name(
            "'Jamie Thingelstad' via rwbookclub"
        ) == "jamie"

    def test_mere_oliver_mention_on_mailing_list_is_model_candidate(self, fresh_db):
        fresh_db.link_member_email("tom@tomeri.org", "tom")
        decision = email_policy.inbound_decision(
            msg(
                from_email="tom@tomeri.org",
                to=[config.BOOK_CLUB_MAILING_LIST_ADDRESS],
                cc=[config.BOOK_CLUB_MAILING_LIST_ADDRESS],
                reply_to=[config.BOOK_CLUB_MAILING_LIST_ADDRESS],
                subject="Re: [rwbookclub] Meeting in 5 days!",
                text=(
                    "Not sure I've seen anything from Oliver recently. "
                    "But (as I told Oliver) I will miss it. I'll be hiking in the Sierra."
                ),
            )
        )
        assert decision.allowed is True
        assert decision.reason == "mailing_list_candidate"
        assert decision.member_slug == "tom"

    def test_current_message_text_strips_html_blockquotes(self, fresh_db):
        fresh_db.link_member_email("tom@tomeri.org", "tom")
        text = email_policy.current_message_text(
            '<html><body><p>I will miss it.</p><blockquote type="cite">'
            "On Jun 25, Oliver wrote:<br>Anything I should answer?"
            "</blockquote></body></html>"
        )
        assert text == "I will miss it."

    def test_question_about_oliver_is_model_candidate(self):
        decision = email_policy.inbound_decision(
            msg(
                from_email="member@example.test",
                to=[config.BOOK_CLUB_MAILING_LIST_ADDRESS],
                subject="Check-in",
                text="Does anyone know whether Oliver emailed Nick?",
            )
        )
        assert decision.allowed is True
        assert decision.reason == "mailing_list_candidate"

    def test_current_message_text_strips_quoted_question(self):
        text = email_policy.current_message_text(
            "\n\nOn Jun 25 Jamie wrote:\n> What did we read about North Korea?"
        )
        assert text == ""


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
