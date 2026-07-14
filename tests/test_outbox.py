"""Durable outbound intent, idempotency, retry, and crash recovery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent import outbox


class RetryableProviderError(RuntimeError):
    pass


def _time(offset: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat()


def test_duplicate_enqueue_and_delivery_call_provider_once(fresh_db):
    payload = {"channel_id": "123", "content": "hello"}
    first = outbox.enqueue(kind="discord", payload=payload, idempotency_key="discord:test")
    second = outbox.enqueue(kind="discord", payload=payload, idempotency_key="discord:test")
    assert first["id"] == second["id"]

    calls = []
    delivered = outbox.deliver_sync(first, lambda: calls.append(1) or {"messageId": "m1"})
    repeated = outbox.deliver_sync(second, lambda: calls.append(2) or {"messageId": "m2"})
    assert delivered == repeated == {"messageId": "m1"}
    assert calls == [1]
    assert fresh_db.outbox_by_key("discord:test")["status"] == "delivered"


def test_idempotency_key_rejects_a_different_payload(fresh_db):
    outbox.enqueue(kind="discord", payload={"content": "one"}, idempotency_key="same")
    with pytest.raises(ValueError, match="different payload"):
        outbox.enqueue(kind="discord", payload={"content": "two"}, idempotency_key="same")


def test_retryable_failure_uses_backoff_then_delivers(fresh_db):
    row = outbox.enqueue(
        kind="email", payload={"subject": "test"}, idempotency_key="email:retry",
        max_attempts=3,
    )
    with pytest.raises(RetryableProviderError):
        outbox.deliver_sync(
            row,
            lambda: (_ for _ in ()).throw(RetryableProviderError("provider rejected")),
            retryable_errors=(RetryableProviderError,),
            retry_delay_seconds=1,
        )
    failed = fresh_db.outbox_by_key("email:retry")
    assert failed["status"] == "retry" and failed["attempts"] == 1
    with fresh_db.connect() as conn:
        conn.execute("UPDATE outbox_messages SET available_at=? WHERE id=?", (_time(-1), row["id"]))
    result = outbox.deliver_sync(
        failed,
        lambda: {"emailId": "e1"},
        retryable_errors=(RetryableProviderError,),
    )
    assert result == {"emailId": "e1"}
    assert fresh_db.outbox_by_key("email:retry")["status"] == "delivered"


def test_retry_exhaustion_becomes_terminal(fresh_db):
    row = outbox.enqueue(
        kind="email", payload={"subject": "test"}, idempotency_key="email:dead",
        max_attempts=1,
    )
    with pytest.raises(RetryableProviderError):
        outbox.deliver_sync(
            row,
            lambda: (_ for _ in ()).throw(RetryableProviderError("permanent")),
            retryable_errors=(RetryableProviderError,),
        )
    assert fresh_db.outbox_by_key("email:dead")["status"] == "dead"
    with pytest.raises(outbox.DeliveryDead):
        outbox.deliver_sync(row, lambda: {"emailId": "must-not-send"})


def test_expired_pre_send_claim_is_safe_to_retry(fresh_db):
    row = outbox.enqueue(kind="discord", payload={"content": "x"},
                         idempotency_key="discord:crash-before")
    claimed = fresh_db.claim_outbox(
        row["idempotency_key"], worker_id="dead-worker",
        now=_time(1), lease_expires_at=_time(-5),
    )
    assert claimed and claimed["status"] == "claimed"
    assert fresh_db.recover_expired_outbox(now=_time())["retry"] == 1

    calls = []
    result = outbox.deliver_sync(row, lambda: calls.append(1) or {"messageId": "m1"})
    assert result == {"messageId": "m1"} and calls == [1]


def test_expired_in_provider_attempt_is_quarantined_not_resent(fresh_db):
    row = outbox.enqueue(kind="email", payload={"subject": "x"},
                         idempotency_key="email:crash-after")
    claimed = fresh_db.claim_outbox(
        row["idempotency_key"], worker_id="dead-worker",
        now=_time(1), lease_expires_at=_time(-5),
    )
    assert fresh_db.mark_outbox_delivering(claimed["id"], worker_id="dead-worker")
    assert fresh_db.recover_expired_outbox(now=_time())["uncertain"] == 1

    calls = []
    with pytest.raises(outbox.DeliveryUncertain):
        outbox.deliver_sync(row, lambda: calls.append(1) or {"emailId": "duplicate"})
    assert calls == []
