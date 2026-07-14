"""Daily off-machine backup: consistent snapshot, once-per-day gate, retention, loud failure."""

import gzip
import sqlite3
import stat

from agent import backup, config


def _point_at(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OFFSITE_BACKUP_DIR", str(tmp_path / "icloud"))
    monkeypatch.setattr(config, "OFFSITE_BACKUP_ENABLED", True)
    monkeypatch.setattr(config, "OFFSITE_BACKUP_KEEP", 3)


def test_backup_writes_valid_snapshot_and_gates_daily(fresh_db, monkeypatch, tmp_path):
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
    with sqlite3.connect(restored) as conn:
        n = conn.execute("SELECT COUNT(*) FROM club_books").fetchone()[0]
    assert n > 100

    # Same day → no-op; force → runs anyway.
    assert backup.run() is None
    assert backup.run(force=True) is not None
    assert (fresh_db.get_job_state(backup.JOB_KEY) or {}).get("file") == out["file"]


def test_backup_retention_prunes_oldest(fresh_db, monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path)
    icloud = tmp_path / "icloud"
    icloud.mkdir()
    for d in ("2026-06-01", "2026-06-02", "2026-06-03"):
        (icloud / f"oliver-{d}.db.gz").write_bytes(b"old")
    backup.run()
    kept = sorted(p.name for p in icloud.glob("oliver-*.db.gz"))
    assert len(kept) == 3                      # KEEP=3
    assert "oliver-2026-06-01.db.gz" not in kept  # oldest pruned
    # conftest freezes the club clock at 2026-06-29 — that's "today's" snapshot name
    assert "oliver-2026-06-29.db.gz" in kept


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
