"""Weekly off-machine backup of oliver.db to iCloud Drive.

The local `agent/backups/` snapshots protect against bad migrations, but everything lived on
one Mac — a dead disk took the club's 23-year record with it. This job writes a consistent
gzipped snapshot into iCloud Drive (`~/Library/Mobile Documents/com~apple~CloudDocs/Oliver/`),
and iCloud carries it off the machine.

Mechanics: the hourly scheduler calls `run()` on every tick; a `job_state['offsite_backup']`
date gate makes it once every seven club-local days after the last successful backup,
whenever the first eligible tick lands (a sleeping Mac catches up when awake). The snapshot uses
sqlite3's online backup API (consistent under WAL, same as admin.sh), gzips to ~3-4 MB, and
prunes to the newest OLIVER_OFFSITE_BACKUP_KEEP files. Success is quiet (one INFO line),
unless the prior successful snapshot disappeared before the next run; that recovery-retention
loss posts a warning to #oliver-log. Write failures do the same, so neither condition can rot
silently.
"""

from __future__ import annotations

import gzip
import logging
import sqlite3
import tempfile
from contextlib import closing
from datetime import date
from pathlib import Path

from agent import clock, config, db, security

log = logging.getLogger("oliver.backup")

JOB_KEY = "offsite_backup"


def _target_dir() -> Path:
    return Path(config.OFFSITE_BACKUP_DIR).expanduser()


def _snapshot_to(dst: Path) -> None:
    """A consistent online snapshot of oliver.db → dst (WAL-safe, same API admin.sh uses)."""
    # A sqlite3 connection context commits/rolls back but does not close the connection.
    with closing(sqlite3.connect(db.DB_PATH)) as src, closing(sqlite3.connect(dst)) as out:
        src.backup(out)


def run(*, force: bool = False) -> dict | None:
    """One weekly off-machine backup. Returns a summary dict when a backup was written,
    None when disabled or not yet due. Raises nothing — failures are logged and
    reported to the activity feed, then swallowed (the scheduler tick must survive)."""
    if not config.OFFSITE_BACKUP_ENABLED:
        return None
    today_date = clock.club_today()
    today = today_date.isoformat()
    state = db.get_job_state(JOB_KEY) or {}
    try:
        age_days = (today_date - date.fromisoformat(state["date"])).days
    except KeyError, TypeError, ValueError:
        age_days = None
    if not force and age_days is not None and 0 <= age_days < config.OFFSITE_BACKUP_INTERVAL_DAYS:
        return None

    target = _target_dir()
    name = f"oliver-{today}.db.gz"
    try:
        security.set_private_umask()
        target.mkdir(parents=True, exist_ok=True, mode=security.PRIVATE_DIR_MODE)
        security.secure_directory_tree(target)
        # A fresh backup alone is not enough to prove recoverability. If the last successful
        # copy disappeared between weekly runs, preserve the new snapshot but make that external
        # retention loss visible to the operator.
        previous_file = state.get("file")
        previous_missing = bool(
            previous_file and state.get("date") != today and not (target / previous_file).is_file()
        )
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            _snapshot_to(tmp_path)
            with open(tmp_path, "rb") as raw, gzip.open(target / name, "wb", compresslevel=6) as gz:
                while chunk := raw.read(1 << 20):
                    gz.write(chunk)
            security.secure_file(target / name)
        finally:
            tmp_path.unlink(missing_ok=True)

        # Retention: newest N by name (names embed the ISO date, so lexical == chronological).
        snapshots = sorted(target.glob("oliver-*.db.gz"), reverse=True)
        for old in snapshots[config.OFFSITE_BACKUP_KEEP :]:
            old.unlink(missing_ok=True)

        size = (target / name).stat().st_size
        db.set_job_state(JOB_KEY, {"date": today, "file": name, "bytes": size})
        if previous_missing:
            db.add_activity(
                "warning",
                "Offsite backup retention degraded",
                f"Previous successful snapshot {previous_file} was missing before {name} ran.",
            )
        log.info(
            "offsite backup written: %s (%.1f MB, keeping %d)",
            target / name,
            size / 1e6,
            min(len(snapshots), config.OFFSITE_BACKUP_KEEP),
        )
        return {"file": name, "bytes": size}
    except OSError as e:
        # Loud failure: iCloud dir missing/unwritable must reach #oliver-log, not just the log file.
        log.exception("offsite backup failed")
        db.add_activity(
            "warning",
            "Offsite backup failed",
            f"Writing {name} to {target} raised {type(e).__name__}: {e}",
        )
        return None
