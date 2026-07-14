"""Durable, idempotent delivery state machine for Oliver's external side effects.

An intent is committed before a provider call. Safe pre-provider failures can retry with bounded
backoff. Once a provider attempt begins, an unclassified exception or expired worker lease is
quarantined as ``uncertain`` rather than automatically risking a duplicate member communication.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from agent import db


LEASE_SECONDS = 120
MAX_RETRY_DELAY_SECONDS = 3600


class OutboxError(RuntimeError):
    pass


class DeliveryDeferred(OutboxError):
    pass


class DeliveryUncertain(OutboxError):
    pass


class DeliveryDead(OutboxError):
    pass


def canonical_payload(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_key(kind: str, payload: dict, explicit: str | None = None) -> str:
    if explicit:
        key = explicit.strip()
        if not key or len(key) > 240:
            raise ValueError("outbox idempotency key must be 1-240 characters")
        return key
    digest = hashlib.sha256(canonical_payload(payload).encode()).hexdigest()
    return f"{kind}:content:{digest}"


def enqueue(*, kind: str, payload: dict, idempotency_key: str | None = None,
            max_attempts: int = 5) -> dict:
    key = stable_key(kind, payload, idempotency_key)
    return db.enqueue_outbox(
        idempotency_key=key,
        kind=kind,
        payload_json=canonical_payload(payload),
        max_attempts=max_attempts,
    )


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:10]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _provider_result(row: dict) -> dict:
    return json.loads(row.get("provider_ref_json") or "{}")


def _existing_result(row: dict) -> dict | None:
    status = row["status"]
    if status == "delivered":
        return _provider_result(row)
    if status == "uncertain":
        raise DeliveryUncertain(
            f"delivery state is uncertain for idempotency key {row['idempotency_key']}"
        )
    if status == "dead":
        raise DeliveryDead(f"delivery exhausted retries for {row['idempotency_key']}")
    return None


def _claim(row: dict, *, worker_id: str, now: datetime) -> dict:
    claimed = db.claim_outbox(
        row["idempotency_key"],
        worker_id=worker_id,
        now=now.isoformat(),
        lease_expires_at=(now + timedelta(seconds=LEASE_SECONDS)).isoformat(),
    )
    if not claimed:
        current = db.outbox_by_key(row["idempotency_key"]) or row
        existing = _existing_result(current)
        if existing is not None:
            return {"_already_delivered": existing}
        raise DeliveryDeferred(
            f"delivery is already claimed or waiting for retry: {row['idempotency_key']}"
        )
    return claimed


def _retry(outbox_id: int, worker_id: str, attempts: int, exc: BaseException,
           *, retry_delay_seconds: int) -> None:
    delay = min(retry_delay_seconds * (2 ** max(0, attempts - 1)), MAX_RETRY_DELAY_SECONDS)
    now = _now()
    db.mark_outbox_retry(
        outbox_id,
        worker_id=worker_id,
        error=f"{type(exc).__name__}: {exc}",
        available_at=(now + timedelta(seconds=delay)).isoformat(),
        now=now.isoformat(),
    )


def deliver_sync(row: dict, deliver: Callable[[], dict], *,
                 retryable_errors: tuple[type[BaseException], ...] = (),
                 retry_delay_seconds: int = 60) -> dict:
    existing = _existing_result(row)
    if existing is not None:
        return existing
    db.recover_expired_outbox()
    worker_id = _worker_id()
    claimed = _claim(row, worker_id=worker_id, now=_now())
    if "_already_delivered" in claimed:
        return claimed["_already_delivered"]
    if not db.mark_outbox_delivering(claimed["id"], worker_id=worker_id):
        raise DeliveryDeferred(f"lost outbox claim for {claimed['idempotency_key']}")
    try:
        result = deliver()
    except retryable_errors as exc:
        _retry(claimed["id"], worker_id, claimed["attempts"], exc,
               retry_delay_seconds=retry_delay_seconds)
        raise
    except BaseException as exc:
        db.mark_outbox_uncertain(
            claimed["id"], worker_id=worker_id, error=f"{type(exc).__name__}: {exc}"
        )
        raise
    encoded = canonical_payload(result or {})
    if not db.mark_outbox_delivered(
        claimed["id"], worker_id=worker_id, provider_ref_json=encoded
    ):
        raise DeliveryUncertain(
            f"provider returned but delivery receipt was not recorded for {claimed['idempotency_key']}"
        )
    return result or {}


async def deliver_async(row: dict, deliver: Callable[[], Awaitable[dict]], *,
                        retryable_errors: tuple[type[BaseException], ...] = (),
                        retry_delay_seconds: int = 60) -> dict:
    existing = _existing_result(row)
    if existing is not None:
        return existing
    await asyncio.to_thread(db.recover_expired_outbox)
    worker_id = _worker_id()
    claimed = await asyncio.to_thread(_claim, row, worker_id=worker_id, now=_now())
    if "_already_delivered" in claimed:
        return claimed["_already_delivered"]
    marked = await asyncio.to_thread(
        db.mark_outbox_delivering, claimed["id"], worker_id=worker_id
    )
    if not marked:
        raise DeliveryDeferred(f"lost outbox claim for {claimed['idempotency_key']}")
    try:
        result = await deliver()
    except retryable_errors as exc:
        await asyncio.to_thread(
            _retry, claimed["id"], worker_id, claimed["attempts"], exc,
            retry_delay_seconds=retry_delay_seconds,
        )
        raise
    except BaseException as exc:
        await asyncio.to_thread(
            db.mark_outbox_uncertain,
            claimed["id"],
            worker_id=worker_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    encoded = canonical_payload(result or {})
    marked = await asyncio.to_thread(
        db.mark_outbox_delivered,
        claimed["id"],
        worker_id=worker_id,
        provider_ref_json=encoded,
    )
    if not marked:
        raise DeliveryUncertain(
            f"provider returned but delivery receipt was not recorded for {claimed['idempotency_key']}"
        )
    return result or {}
