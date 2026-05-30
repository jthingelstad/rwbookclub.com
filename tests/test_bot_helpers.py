"""Pure helpers in agent/bot.py — no Discord required."""

from __future__ import annotations

import pytest

from agent.bot import _channel_mode, _is_addressed, _strip_address


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
