"""Club-wide cadence send helpers: chunking + mailing-list + Discord mirror."""
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agent import clock, commands, config


_TZ = ZoneInfo(config.CLUB_TIMEZONE)


def test_meeting_datetime_honors_start_time():
    # A 6:30pm meeting → the aware start is 18:30 local, not midnight.
    dt = commands._meeting_datetime({"date": "2026-06-30", "startTime": "18:30"})
    assert dt == datetime(2026, 6, 30, 18, 30, tzinfo=_TZ)


def test_meeting_datetime_defaults_to_evening_when_time_unknown():
    # No start_time → fall back to the evening default, never midnight.
    dt = commands._meeting_datetime({"date": "2026-06-30", "startTime": None})
    assert dt == datetime(2026, 6, 30, clock.DEFAULT_MEETING_HOUR, 0, tzinfo=_TZ)


def test_meeting_datetime_none_on_bad_date():
    assert commands._meeting_datetime({"date": "not-a-date"}) is None


def test_two_day_bound_honors_time_not_midnight():
    # "2 days before" is bounded against the meeting's TIME: the midnight heartbeat two days
    # before is still too early; only at/after the meeting hour does the window open.
    meeting_dt = commands._meeting_datetime({"date": "2026-06-30", "startTime": "18:30"})
    open_at = meeting_dt - timedelta(days=2)
    midnight_two_days_before = datetime(2026, 6, 28, 0, 0, tzinfo=_TZ)
    assert midnight_two_days_before < open_at          # midnight is BEFORE the window → no send
    assert datetime(2026, 6, 28, 18, 30, tzinfo=_TZ) >= open_at   # the meeting hour opens it


def test_chunk_respects_limit():
    text = "\n".join(f"line number {i}" for i in range(200))
    chunks = commands._chunk(text, 50)
    assert chunks
    assert all(len(c) <= 50 for c in chunks)


def test_chunk_short_text_is_one_piece():
    assert commands._chunk("hello", 2000) == ["hello"]


class _Channel:
    def __init__(self):
        self.posts = []

    async def send(self, text):
        self.posts.append(text)


class _Client:
    def __init__(self, channel):
        self._channel = channel

    def get_channel(self, _id):
        return self._channel


def test_send_club_email_targets_list_and_mirrors_to_discord(monkeypatch):
    sent = {}
    monkeypatch.setattr(commands.outbound, "finalize", lambda body: body + "\n\n— Oliver")
    monkeypatch.setattr(commands.outbound, "send", lambda **kw: sent.update(kw) or {"emailId": "e1"})
    channel = _Channel()
    monkeypatch.setattr(commands, "_client", _Client(channel))
    monkeypatch.setattr(config, "MAIN_CHANNEL_ID", 123)

    asyncio.run(commands._send_club_email("Subject", "The body"))

    # Emailed to the whole mailing list, already-finalized (no double signature).
    assert sent["to"] == [config.BOOK_CLUB_MAILING_LIST_ADDRESS]
    assert sent["sign"] is False
    assert sent["body"] == "The body\n\n— Oliver"
    # Mirrored to Discord with the same finalized content.
    assert channel.posts and "The body" in channel.posts[0]
    assert "— Oliver" in "".join(channel.posts)
