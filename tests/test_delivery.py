"""The hourly provider worker drains persisted email and Discord intents."""

import asyncio

from agent import delivery, outbox
from agent.mail import outbound


class _Message:
    id = 991


class _Channel:
    id = 123

    def __init__(self):
        self.posts = []

    async def send(self, content):
        self.posts.append(content)
        return _Message()


class _Client:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel if channel_id == self.channel.id else None


def test_drain_delivers_persisted_email_and_discord_once(fresh_db, monkeypatch):
    email_payload = {
        "to": ["person@example.test"],
        "subject": "Hello",
        "body": "Body",
        "html_body": None,
        "cc": None,
        "in_reply_to": None,
        "references": None,
        "policy": "trusted",
    }
    outbox.enqueue(kind="email", payload=email_payload, idempotency_key="email:drain")
    outbox.enqueue(
        kind="discord",
        payload={"channel_id": "123", "content": "Hello Discord"},
        idempotency_key="discord:drain",
    )
    emails = []
    monkeypatch.setattr(
        outbound.email_jmap,
        "send_email",
        lambda **kwargs: emails.append(kwargs) or {"emailId": "e1"},
    )
    channel = _Channel()
    client = _Client(channel)

    assert asyncio.run(delivery.drain(client)) == 2
    assert asyncio.run(delivery.drain(client)) == 0
    assert len(emails) == 1
    assert channel.posts == ["Hello Discord"]
    assert fresh_db.outbox_by_key("email:drain")["status"] == "delivered"
    assert fresh_db.outbox_by_key("discord:drain")["status"] == "delivered"
