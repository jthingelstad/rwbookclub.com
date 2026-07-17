"""Pure helpers in agent/bot.py — no Discord required."""

from __future__ import annotations

import logging

import pytest

from agent.bot import (
    _channel_mode,
    _is_addressed,
    _strip_address,
)
from agent.mail.email_jmap import InboundEmail
from agent.mail.inbound import record_ignored_email, roll_call_status_from_email


class TestIsAddressed:
    def test_mention_only(self):
        assert _is_addressed(True, False, False)

    def test_name_only(self):
        assert _is_addressed(False, True, False)

    def test_reply_only(self):
        assert _is_addressed(False, False, True)

    def test_none(self):
        assert not _is_addressed(False, False, False)

    def test_combinations(self):
        assert _is_addressed(True, True, True)
        assert _is_addressed(True, False, True)


class TestStripAddress:
    def test_bare_mention(self):
        assert _strip_address("<@123> what's up", 123) == "what's up"

    def test_nickname_mention(self):
        # Discord nickname mentions use <@!id>; both forms should strip.
        assert _strip_address("<@!123> hello", 123) == "hello"

    def test_no_mention(self):
        assert _strip_address("just a question", 123) == "just a question"

    def test_other_bot_mention_preserved(self):
        # A different bot's mention should not be stripped.
        assert _strip_address("<@999> hi", 123) == "<@999> hi"

    def test_strips_surrounding_whitespace(self):
        assert _strip_address("   <@123>  query  ", 123) == "query"


class TestChannelMode:
    def test_ask_channel_answers(self):
        assert _channel_mode(10, ask_id=10, monitored_ids={20, 30}) == "answer"

    def test_monitored_channel(self):
        assert _channel_mode(20, ask_id=10, monitored_ids={20, 30}) == "monitored"

    def test_unknown_channel_ignored(self):
        assert _channel_mode(99, ask_id=10, monitored_ids={20, 30}) == "ignore"

    def test_no_ask_channel_is_dev_fallback(self):
        # With no ask channel configured, Oliver answers everywhere (dev mode).
        assert _channel_mode(99, ask_id=0, monitored_ids=set()) == "answer"


class TestIgnoredEmailLogging:
    def test_records_activity_and_process_log(self, fresh_db, caplog):
        msg = InboundEmail(
            id="m1",
            thread_id="t1",
            message_id="msg1@example.test",
            from_name="Google Groups",
            from_email="noreply@groups.google.com",
            to=["oliver@rwbookclub.com"],
            cc=[],
            reply_to=[],
            subject="Invitation to join rwbookclub",
            text="Invite text",
            received_at="2026-06-25T13:00:00Z",
            references=[],
        )
        with caplog.at_level(logging.INFO, logger="oliver"):
            record_ignored_email(msg, "sender_not_allowed")

        rows = fresh_db.pending_activity()
        assert len(rows) == 1
        assert rows[0]["kind"] == "email_ignored"
        assert rows[0]["title"] == "Email ignored"
        assert "From: Google Groups <noreply@groups.google.com>" in rows[0]["body"]
        assert "Subject: Invitation to join rwbookclub" in rows[0]["body"]
        assert "Reason: sender_not_allowed" in rows[0]["body"]
        assert "Ignored email m1 from noreply@groups.google.com: sender_not_allowed" in caplog.text


class TestRollCallEmailParsing:
    @pytest.mark.parametrize("body,status", [
        ("yes, I can make it.\n\n--\nJamie", "yes"),
        ("No.\n\nOn Jun 9 Oliver wrote:", "no"),
        ("I can't make it this month.", "no"),
        ("Unsure for now.", "unsure"),
    ])
    def test_explicit_roll_call_replies(self, body, status):
        assert roll_call_status_from_email(
            "Re: Roll call: A World Appears on 2026-06-30",
            body,
        ) == status

    def test_ignores_non_roll_call_subjects(self):
        assert roll_call_status_from_email(
            "Re: Reading check-in: A World Appears",
            "Yes, I can make it.",
        ) is None

    def test_ignores_quoted_history_after_blank(self):
        assert roll_call_status_from_email(
            "Re: Roll call: A World Appears on 2026-06-30",
            "\n\nOn Jun 9 Oliver wrote:\n> Can you make it?\n> Yes",
        ) is None
