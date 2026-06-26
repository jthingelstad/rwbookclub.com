"""Club-wide cadence send helpers: chunking + mailing-list + Discord mirror."""
import asyncio

from agent import commands, config


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
