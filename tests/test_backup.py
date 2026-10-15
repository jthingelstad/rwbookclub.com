"""Weekly off-machine backup: consistent snapshot, seven-day gate, retention, loud failure."""

import gzip
import sqlite3
import stat
from contextlib import closing
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from agent import backup, clock, config


def _point_at(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OFFSITE_BACKUP_DIR", str(tmp_path / "icloud"))
    monkeypatch.setattr(config, "OFFSITE_BACKUP_ENABLED", True)
    monkeypatch.setattr(config, "OFFSITE_BACKUP_KEEP", 3)


def test_backup_writes_valid_snapshot_and_gates_weekly(fresh_db, monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path)
    out = backup.run()
    assert out and out["file"].startswith("oliver-") and out["bytes"] > 0
    target = tmp_path / "icloud" / out["file"]
    assert target.exists()
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600

    # The gzip really is a working SQLite database with the club record inside.
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(target.read_bytes()))
    with closing(sqlite3.connect(restored)) as conn:
        n = conn.execute("SELECT COUNT(*) FROM club_books").fetchone()[0]
    assert n > 100

    # Same day → no-op; force → runs anyway.
    assert backup.run() is None
    assert backup.run(force=True) is not None
    assert (fresh_db.get_job_state(backup.JOB_KEY) or {}).get("file") == out["file"]


@pytest.mark.parametrize(
    ("last_date", "due"),
    [
        ("2026-06-29", False),
        ("2026-06-28", False),  # A new day is no longer a new backup.
        ("2026-06-23", False),
        ("2026-06-22", True),  # Exactly seven club-local days.
        ("2026-06-01", True),  # Catch up after downtime.
        ("2026-07-01", True),  # A future/bad receipt must not suppress backups.
        ("invalid", True),
    ],
)
def test_weekly_due_gate(fresh_db, monkeypatch, tmp_path, last_date, due):
    _point_at(monkeypatch, tmp_path)
    fresh_db.set_job_state(backup.JOB_KEY, {"date": last_date})
    assert bool(backup.run()) is due


def test_weekly_gate_uses_club_date_across_dst_and_year_boundary(fresh_db, monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path)
    # Six days plus a DST hour is still fewer than seven club-local days.
    fresh_db.set_job_state(backup.JOB_KEY, {"date": "2026-10-27"})
    monkeypatch.setattr(
        clock, "club_now", lambda: datetime(2026, 11, 2, 23, 30, tzinfo=ZoneInfo("America/Chicago"))
    )
    assert backup.run() is None
    monkeypatch.setattr(clock, "club_today", lambda: date(2026, 11, 3))
    assert backup.run() is not None
    fresh_db.set_job_state(backup.JOB_KEY, {"date": "2026-12-28"})
    monkeypatch.setattr(clock, "club_today", lambda: date(2027, 1, 4))
    assert backup.run() is not None


def test_backup_retention_prunes_oldest(fresh_db, monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path)
    icloud = tmp_path / "icloud"
    icloud.mkdir()
    for d in ("2026-06-01", "2026-06-02", "2026-06-03"):
        (icloud / f"oliver-{d}.db.gz").write_bytes(b"old")
    backup.run()
    kept = sorted(p.name for p in icloud.glob("oliver-*.db.gz"))
    assert len(kept) == 3  # KEEP=3
    assert "oliver-2026-06-01.db.gz" not in kept  # oldest pruned
    # conftest freezes the club clock at 2026-06-29 — that's "today's" snapshot name
    assert "oliver-2026-06-29.db.gz" in kept


def test_backup_warns_when_the_prior_successful_snapshot_disappeared(
    fresh_db, monkeypatch, tmp_path
):
    _point_at(monkeypatch, tmp_path)
    fresh_db.set_job_state(
        backup.JOB_KEY,
        {"date": "2026-06-28", "file": "oliver-2026-06-28.db.gz", "bytes": 123},
    )

    assert backup.run(force=True)

    warnings = fresh_db.pending_activity(limit=5)
    assert any(
        row["kind"] == "warning" and row["title"] == "Offsite backup retention degraded"
        for row in warnings
    )


def test_backup_disabled_and_failure_paths(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OFFSITE_BACKUP_ENABLED", False)
    assert backup.run() is None

    _point_at(monkeypatch, tmp_path)
    # Unwritable target → returns None, records a warning activity, does NOT raise.
    blocker = tmp_path / "icloud"
    blocker.write_text("a file where the dir should go")
    assert backup.run() is None
    acts = fresh_db.pending_activity(limit=5)
    assert any(a["kind"] == "warning" and "backup" in a["title"].lower() for a in acts)
    assert fresh_db.get_job_state(backup.JOB_KEY) is None
