"""Provider adapters for draining Oliver's durable outbound outbox.

Decision code only persists intent and asks the narrow adapter to attempt delivery. The hourly
scheduler also drains safe pending/retry rows left by a crash or a known retryable provider error.
"""

from __future__ import annotations

import asyncio
import json
import logging

from agent import db, outbox
from agent.mail import outbound

log = logging.getLogger("oliver.delivery")


async def deliver_discord_row(row: dict, channel) -> dict:
    payload = json.loads(row["payload_json"])

    async def _deliver() -> dict:
        message = await channel.send(payload["content"])
        message_id = getattr(message, "id", None)
        return {"messageId": str(message_id)} if message_id is not None else {}

    # Discord HTTP failures may be ambiguous, so none are automatically classified as retryable.
    return await outbox.deliver_async(row, _deliver)


async def drain(client, *, limit: int = 20) -> int:
    """Deliver safe queued work. Returns the number newly confirmed delivered."""
    await asyncio.to_thread(db.recover_expired_outbox)
    rows = await asyncio.to_thread(db.pending_outbox, limit=limit)
    delivered = 0
    for row in rows:
        try:
            if row["kind"] == "email":
                await asyncio.to_thread(outbound.deliver_outbox_row, row)
            elif row["kind"] in {"discord", "discord_reply"}:
                payload = json.loads(row["payload_json"])
                try:
                    channel_id = int(payload["channel_id"])
                except (KeyError, TypeError, ValueError):
                    log.error("outbox %s has no valid Discord channel id", row["id"])
                    continue
                channel = client.get_channel(channel_id)
                if channel is None:
                    # No provider call happened. Leave pending for a future cache/config recovery.
                    log.warning("outbox %s: Discord channel %s not found", row["id"], channel_id)
                    continue
                # A recovered interactive reply is sent as a normal channel post. The original
                # immediate attempt preserves reply threading; recovery prioritizes exactly-once.
                await deliver_discord_row(row, channel)
            else:
                log.error("outbox %s has unsupported kind %r", row["id"], row["kind"])
                continue
        except outbox.DeliveryDeferred:
            continue
        except Exception:
            # The state machine has already recorded retry/uncertain/dead where appropriate.
            log.exception("outbox delivery failed for %s (%s)", row["id"], row["kind"])
            continue
        delivered += 1
    return delivered
