"""Pure helpers in agent/commands.py."""

from __future__ import annotations

from agent.commands import _should_start_scheduled_roll_call


def test_scheduled_roll_call_skips_ready_meeting():
    status = {"recommendation": "ready"}

    assert not _should_start_scheduled_roll_call(status, roll_call=None)


def test_scheduled_roll_call_skips_existing_discord_roll_call():
    status = {"recommendation": "needs_attention"}
    roll_call = {"status": "open", "message_id": "msg1"}

    assert not _should_start_scheduled_roll_call(status, roll_call)


def test_scheduled_roll_call_starts_when_not_ready_and_no_discord_prompt():
    status = {"recommendation": "needs_attention"}

    assert _should_start_scheduled_roll_call(status, roll_call=None)
    assert _should_start_scheduled_roll_call(status, {"status": "open", "message_id": None})
