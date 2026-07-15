"""The scheduler coordinator composes independently testable runtime stages."""

from __future__ import annotations

import asyncio

from agent import commands, config


class _Client:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel if channel_id else None


def test_scheduler_coordinator_counts_each_stage(monkeypatch):
    calls = []
    channel = object()
    monkeypatch.setattr(commands, "_client", _Client(channel))
    monkeypatch.setattr(config, "MAIN_CHANNEL_ID", 123)

    async def maintenance(now):
        calls.append("maintenance")
        return 2

    async def notifications(main, now):
        assert main is channel
        calls.append("notifications")
        return 3

    async def meetings(main, now):
        assert main is channel
        calls.append("meetings")
        return 4

    async def reminders():
        calls.append("reminders")
        return 5

    monkeypatch.setattr(commands, "_run_maintenance_jobs", maintenance)
    monkeypatch.setattr(commands, "_post_due_notifications", notifications)
    monkeypatch.setattr(commands, "_run_meeting_jobs", meetings)
    monkeypatch.setattr(commands, "_post_due_reminders", reminders)

    assert asyncio.run(commands._run_scheduler_unleased()) == 14
    assert calls == ["maintenance", "notifications", "meetings", "reminders"]


def test_scheduler_still_runs_reminders_without_main_channel(monkeypatch):
    calls = []
    monkeypatch.setattr(commands, "_client", _Client(None))
    monkeypatch.setattr(config, "MAIN_CHANNEL_ID", 0)

    async def maintenance(now):
        calls.append("maintenance")
        return 1

    async def reminders():
        calls.append("reminders")
        return 2

    monkeypatch.setattr(commands, "_run_maintenance_jobs", maintenance)
    monkeypatch.setattr(commands, "_post_due_reminders", reminders)

    assert asyncio.run(commands._run_scheduler_unleased()) == 3
    assert calls == ["maintenance", "reminders"]
