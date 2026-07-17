"""Persistent scheduler leases: exclusion, takeover, outcomes, and privacy-safe status."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from agent import jobs

BASE = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


def _at(seconds: int) -> str:
    return (BASE + timedelta(seconds=seconds)).isoformat()


def test_active_owner_excludes_concurrent_run_and_expiry_allows_takeover(fresh_db):
    first = fresh_db.begin_job_run(
        "review_drive",
        lease_owner="worker-one",
        lease_expires_at=_at(30),
        expected_interval_seconds=3600,
        now=_at(0),
    )
    assert first
    assert (
        fresh_db.begin_job_run(
            "review_drive",
            lease_owner="worker-two",
            lease_expires_at=_at(40),
            expected_interval_seconds=3600,
            now=_at(10),
        )
        is None
    )

    takeover = fresh_db.begin_job_run(
        "review_drive",
        lease_owner="worker-two",
        lease_expires_at=_at(80),
        expected_interval_seconds=3600,
        now=_at(31),
    )
    assert takeover
    abandoned = fresh_db.job_run(first["run_id"])
    assert abandoned["outcome"] == "abandoned"
    assert abandoned["error"] == "lease_expired"


def test_success_and_failure_status_is_bounded_and_content_free(fresh_db):
    success = fresh_db.begin_job_run(
        "health_digest",
        lease_owner="worker-one",
        lease_expires_at=_at(30),
        expected_interval_seconds=120,
        now=_at(0),
    )
    assert fresh_db.finish_job_run(
        success["run_id"],
        job_name="health_digest",
        lease_owner="worker-one",
        outcome="succeeded",
        duration_ms=12,
        processed_count=1,
        now=_at(2),
    )
    failed = fresh_db.begin_job_run(
        "health_digest",
        lease_owner="worker-two",
        lease_expires_at=_at(40),
        expected_interval_seconds=120,
        now=_at(3),
    )
    assert fresh_db.finish_job_run(
        failed["run_id"],
        job_name="health_digest",
        lease_owner="worker-two",
        outcome="failed",
        duration_ms=8,
        error="RuntimeError",
        now=_at(4),
    )

    row = fresh_db.job_statuses(now=_at(60))[0]
    assert row["last_success"] == _at(2)
    assert row["last_failure"] == _at(4)
    assert row["last_error"] == "RuntimeError"
    assert row["lease_owner"] is None
    assert row["overdue"] is False
    assert fresh_db.job_statuses(now=_at(123))[0]["overdue"] is True


def test_async_wrapper_records_exception_class_only(fresh_db):
    async def boom():
        raise RuntimeError("member@example.test said private words")

    with pytest.raises(RuntimeError):
        asyncio.run(jobs.run("private_failure", boom))
    row = fresh_db.job_statuses()[0]
    assert row["last_error"] == "RuntimeError"
    assert "example" not in jobs.format_status([row])


def test_async_wrapper_allows_only_one_active_owner(fresh_db):
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocking_work():
            entered.set()
            await release.wait()
            return 3

        first_task = asyncio.create_task(jobs.run("one-owner", blocking_work))
        await entered.wait()
        second = await jobs.run("one-owner", lambda: asyncio.sleep(0, result=9))
        release.set()
        first = await first_task
        return first, second

    first, second = asyncio.run(scenario())
    assert first.executed is True and first.value == 3
    assert second.executed is False
    run = fresh_db.job_run(first.run_id)
    assert run["outcome"] == "succeeded"
    assert run["processed_count"] == 3
