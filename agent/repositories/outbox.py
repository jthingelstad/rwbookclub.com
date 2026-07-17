"""Persistence operations for the durable outbound-delivery state machine."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from sqlite3 import Connection

Connect = Callable[[], AbstractContextManager[Connection]]


def enqueue(
    connect: Connect,
    *,
    now: str,
    idempotency_key: str,
    kind: str,
    payload_json: str,
    max_attempts: int = 5,
) -> dict:
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO outbox_messages "
            "(idempotency_key, kind, payload_json, max_attempts, available_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (idempotency_key, kind, payload_json, max(1, int(max_attempts)), now, now, now),
        )
        row = conn.execute(
            "SELECT * FROM outbox_messages WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if row and (row["kind"] != kind or row["payload_json"] != payload_json):
            raise ValueError("outbox idempotency key was reused with a different payload")
    return dict(row)


def by_key(connect: Connect, idempotency_key: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM outbox_messages WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
    return dict(row) if row else None


def pending(connect: Connect, *, limit: int, now: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM outbox_messages "
            "WHERE status IN ('pending', 'retry') AND available_at <= ? "
            "ORDER BY available_at, id LIMIT ?",
            (now, max(1, min(int(limit), 100))),
        ).fetchall()
    return [dict(row) for row in rows]


def claim(
    connect: Connect, idempotency_key: str, *, worker_id: str, lease_expires_at: str, now: str
) -> dict | None:
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM outbox_messages WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        eligible = bool(
            row
            and (
                (row["status"] in {"pending", "retry"} and row["available_at"] <= now)
                or (row["status"] == "claimed" and (row["lease_expires_at"] or "") <= now)
            )
        )
        if not eligible:
            return None
        conn.execute(
            "UPDATE outbox_messages SET status='claimed', attempts=attempts+1, lease_owner=?, "
            "lease_expires_at=?, updated_at=? WHERE id=?",
            (worker_id, lease_expires_at, now, row["id"]),
        )
        claimed = conn.execute(
            "SELECT * FROM outbox_messages WHERE id = ?", (row["id"],)
        ).fetchone()
    return dict(claimed)


def mark_delivering(connect: Connect, outbox_id: int, *, worker_id: str, now: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE outbox_messages SET status='delivering', updated_at=? "
            "WHERE id=? AND status='claimed' AND lease_owner=?",
            (now, outbox_id, worker_id),
        )
    return cur.rowcount > 0


def mark_delivered(
    connect: Connect, outbox_id: int, *, worker_id: str, provider_ref_json: str, now: str
) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE outbox_messages SET status='delivered', provider_ref_json=?, delivered_at=?, "
            "updated_at=?, lease_owner=NULL, lease_expires_at=NULL, last_error=NULL "
            "WHERE id=? AND status='delivering' AND lease_owner=?",
            (provider_ref_json, now, now, outbox_id, worker_id),
        )
    return cur.rowcount > 0


def mark_retry(
    connect: Connect, outbox_id: int, *, worker_id: str, error: str, available_at: str, now: str
) -> str | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT attempts, max_attempts FROM outbox_messages "
            "WHERE id=? AND status='delivering' AND lease_owner=?",
            (outbox_id, worker_id),
        ).fetchone()
        if not row:
            return None
        status = "dead" if row["attempts"] >= row["max_attempts"] else "retry"
        conn.execute(
            "UPDATE outbox_messages SET status=?, available_at=?, last_error=?, updated_at=?, "
            "lease_owner=NULL, lease_expires_at=NULL WHERE id=?",
            (status, available_at, error[:500], now, outbox_id),
        )
    return status


def mark_uncertain(
    connect: Connect, outbox_id: int, *, worker_id: str, error: str, now: str
) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE outbox_messages SET status='uncertain', last_error=?, updated_at=?, "
            "lease_owner=NULL, lease_expires_at=NULL "
            "WHERE id=? AND status='delivering' AND lease_owner=?",
            (error[:500], now, outbox_id, worker_id),
        )
    return cur.rowcount > 0


def recover_expired(connect: Connect, *, now: str) -> dict:
    """Recover safe pre-send claims and quarantine ambiguous in-flight deliveries."""
    with connect() as conn:
        claimed = conn.execute(
            "UPDATE outbox_messages SET status='retry', available_at=?, updated_at=?, "
            "lease_owner=NULL, lease_expires_at=NULL, "
            "last_error=COALESCE(last_error, 'worker lease expired before provider attempt') "
            "WHERE status='claimed' AND lease_expires_at <= ?",
            (now, now, now),
        ).rowcount
        uncertain = conn.execute(
            "UPDATE outbox_messages SET status='uncertain', updated_at=?, lease_owner=NULL, "
            "lease_expires_at=NULL, "
            "last_error=COALESCE(last_error, 'worker lease expired during provider attempt') "
            "WHERE status='delivering' AND lease_expires_at <= ?",
            (now, now),
        ).rowcount
    return {"retry": claimed, "uncertain": uncertain}


def status_counts(connect: Connect) -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM outbox_messages GROUP BY status"
        ).fetchall()
    return {row["status"]: row["n"] for row in rows}
