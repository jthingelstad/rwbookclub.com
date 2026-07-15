"""Persistence for scheduler state blobs, leases, and run observability."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from sqlite3 import Connection

Connect = Callable[[], AbstractContextManager[Connection]]


def get_state(connect: Connect, key: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM job_state WHERE key = ?", (key,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
        return None


def set_state(connect: Connect, key: str, value: dict, *, now: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO job_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), now),
        )


def begin_run(connect: Connect, job_name: str, *, lease_owner: str,
              lease_expires_at: str, expected_interval_seconds: int, now: str) -> dict | None:
    """Atomically acquire a job lease and open its run row."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        lease = conn.execute(
            "SELECT * FROM job_leases WHERE job_name=?", (job_name,)
        ).fetchone()
        if lease and lease["lease_owner"] and (lease["lease_expires_at"] or "") > now:
            return None
        if lease and lease["lease_owner"]:
            conn.execute(
                "UPDATE job_runs SET outcome='abandoned', finished_at=?, "
                "duration_ms=CAST(MAX(0, (julianday(?) - julianday(started_at)) * 86400000) AS INT), "
                "error='lease_expired' WHERE job_name=? AND lease_owner=? AND outcome='running'",
                (now, now, job_name, lease["lease_owner"]),
            )
        conn.execute(
            "INSERT INTO job_leases "
            "(job_name, lease_owner, lease_expires_at, acquired_at, updated_at, "
            "expected_interval_seconds) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(job_name) DO UPDATE SET lease_owner=excluded.lease_owner, "
            "lease_expires_at=excluded.lease_expires_at, acquired_at=excluded.acquired_at, "
            "updated_at=excluded.updated_at, "
            "expected_interval_seconds=excluded.expected_interval_seconds",
            (job_name, lease_owner, lease_expires_at, now, now,
             max(1, int(expected_interval_seconds))),
        )
        cur = conn.execute(
            "INSERT INTO job_runs (job_name, lease_owner, started_at) VALUES (?, ?, ?)",
            (job_name, lease_owner, now),
        )
    return {"run_id": cur.lastrowid, "job_name": job_name, "lease_owner": lease_owner,
            "started_at": now, "lease_expires_at": lease_expires_at}


def renew_lease(connect: Connect, job_name: str, *, lease_owner: str,
                lease_expires_at: str, now: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE job_leases SET lease_expires_at=?, updated_at=? "
            "WHERE job_name=? AND lease_owner=?",
            (lease_expires_at, now, job_name, lease_owner),
        )
    return cur.rowcount > 0


def finish_run(connect: Connect, run_id: int, *, job_name: str, lease_owner: str,
               outcome: str, duration_ms: int, processed_count: int = 0,
               error: str | None = None, now: str) -> bool:
    if outcome not in {"succeeded", "failed"}:
        raise ValueError("job outcome must be succeeded or failed")
    with connect() as conn:
        cur = conn.execute(
            "UPDATE job_runs SET finished_at=?, outcome=?, duration_ms=?, processed_count=?, "
            "error=? WHERE id=? AND job_name=? AND lease_owner=? AND outcome='running'",
            (now, outcome, max(0, int(duration_ms)), max(0, int(processed_count)),
             (error or "")[:120] or None, run_id, job_name, lease_owner),
        )
        if cur.rowcount:
            conn.execute(
                "UPDATE job_leases SET lease_owner=NULL, lease_expires_at=NULL, updated_at=? "
                "WHERE job_name=? AND lease_owner=?",
                (now, job_name, lease_owner),
            )
    return cur.rowcount > 0


def run_by_id(connect: Connect, run_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM job_runs WHERE id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def statuses(connect: Connect, *, now: str) -> list[dict]:
    """Operational job status without any job payload or member content."""
    now_dt = datetime.fromisoformat(now)
    with connect() as conn:
        leases = conn.execute("SELECT * FROM job_leases ORDER BY job_name").fetchall()
        out = []
        for lease in leases:
            success = conn.execute(
                "SELECT finished_at, duration_ms, processed_count FROM job_runs "
                "WHERE job_name=? AND outcome='succeeded' ORDER BY id DESC LIMIT 1",
                (lease["job_name"],),
            ).fetchone()
            failure = conn.execute(
                "SELECT finished_at, outcome, error FROM job_runs "
                "WHERE job_name=? AND outcome IN ('failed','abandoned') "
                "ORDER BY id DESC LIMIT 1",
                (lease["job_name"],),
            ).fetchone()
            last_success = success["finished_at"] if success else None
            overdue = True
            if last_success:
                elapsed = (now_dt - datetime.fromisoformat(last_success)).total_seconds()
                overdue = elapsed > int(lease["expected_interval_seconds"])
            active = bool(
                lease["lease_owner"] and (lease["lease_expires_at"] or "") > now
            )
            if active:
                overdue = False
            out.append({
                "job_name": lease["job_name"],
                "last_success": last_success,
                "last_duration_ms": success["duration_ms"] if success else None,
                "last_processed_count": success["processed_count"] if success else None,
                "last_failure": failure["finished_at"] if failure else None,
                "last_failure_outcome": failure["outcome"] if failure else None,
                "last_error": failure["error"] if failure else None,
                "lease_owner": lease["lease_owner"] if active else None,
                "lease_expires_at": lease["lease_expires_at"] if active else None,
                "expected_interval_seconds": lease["expected_interval_seconds"],
                "overdue": overdue,
            })
    return out
