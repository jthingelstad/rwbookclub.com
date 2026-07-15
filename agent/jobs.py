"""Persistent ownership and observability for Oliver's scheduled jobs."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from agent import db

DEFAULT_LEASE_SECONDS = 15 * 60
DEFAULT_EXPECTED_INTERVAL_SECONDS = 2 * 60 * 60


@dataclass(frozen=True)
class Result:
    executed: bool
    value: Any = None
    run_id: int | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:10]}"


def _count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, (list, tuple, set)):
        return len(value)
    if isinstance(value, dict):
        for key in ("processed_count", "sent", "members", "enriched"):
            if isinstance(value.get(key), int):
                return max(0, value[key])
    return 0


async def run(job_name: str, work: Callable[[], Awaitable[Any]], *,
              lease_seconds: int = DEFAULT_LEASE_SECONDS,
              expected_interval_seconds: int = DEFAULT_EXPECTED_INTERVAL_SECONDS) -> Result:
    """Run one async job under a renewable lease and write a terminal run record."""
    lease_seconds = max(3, int(lease_seconds))
    owner = _owner()
    started = _now()
    lease = await asyncio.to_thread(
        db.begin_job_run,
        job_name,
        lease_owner=owner,
        lease_expires_at=(started + timedelta(seconds=lease_seconds)).isoformat(),
        expected_interval_seconds=max(1, int(expected_interval_seconds)),
        now=started.isoformat(),
    )
    if lease is None:
        return Result(executed=False)

    async def _heartbeat() -> None:
        while True:
            await asyncio.sleep(max(1, lease_seconds // 3))
            now = _now()
            renewed = await asyncio.to_thread(
                db.renew_job_lease,
                job_name,
                lease_owner=owner,
                lease_expires_at=(now + timedelta(seconds=lease_seconds)).isoformat(),
                now=now.isoformat(),
            )
            if not renewed:
                return

    heartbeat = asyncio.create_task(_heartbeat())
    began = time.monotonic()
    try:
        value = await work()
    except BaseException as exc:
        duration_ms = int((time.monotonic() - began) * 1000)
        # Only the exception class enters the ledger. Full diagnostics remain in owner-only logs,
        # preventing member names, addresses, or message content from leaking into status tooling.
        await asyncio.to_thread(
            db.finish_job_run,
            lease["run_id"],
            job_name=job_name,
            lease_owner=owner,
            outcome="failed",
            duration_ms=duration_ms,
            error=type(exc).__name__,
        )
        raise
    else:
        duration_ms = int((time.monotonic() - began) * 1000)
        await asyncio.to_thread(
            db.finish_job_run,
            lease["run_id"],
            job_name=job_name,
            lease_owner=owner,
            outcome="succeeded",
            duration_ms=duration_ms,
            processed_count=_count(value),
        )
        return Result(executed=True, value=value, run_id=lease["run_id"])
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass


def status() -> list[dict]:
    return db.job_statuses()


def format_status(rows: list[dict] | None = None) -> str:
    rows = status() if rows is None else rows
    if not rows:
        return "No scheduled job runs have been recorded yet."
    lines = []
    for row in rows:
        state = "OVERDUE" if row["overdue"] else "ok"
        if row["lease_owner"]:
            state = f"running until {row['lease_expires_at']}"
        last_ok = row["last_success"] or "never"
        last_fail = row["last_failure"] or "never"
        error = f" ({row['last_error']})" if row["last_error"] else ""
        lines.append(
            f"{row['job_name']}: {state}; success={last_ok}; failure={last_fail}{error}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    from agent import database
    database.initialize()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=["status"], default="status")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    rows = status()
    print(json.dumps(rows, indent=2) if args.json else format_status(rows))
