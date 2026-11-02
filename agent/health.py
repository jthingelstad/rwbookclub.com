"""Oliver's health facts, read every morning by the Otto Ops digest.

`python -m agent.health --json` prints the facts (backup age, warnings, outbox, scheduler jobs,
memory, usage, next meeting) and the problems in them. It only reads: it never initializes or
migrates the database, and makes no model or network call. The Otto Ops job
`post-morning-digest` runs it at 06:30 and posts counts only. This replaced the weekly health
email on 2026-11-02; a stopped Oliver is caught by `check-services-running`, which watches
com.rwbookclub.oliver every 15 minutes.
"""

from __future__ import annotations

import argparse
import json

from agent import clock, config, db, jobs
from agent.club import meeting_rules


def snapshot() -> dict:
    """The facts. Everything here must be cheap and local — no network beyond the DB."""
    backup = db.get_job_state("offsite_backup") or {}
    backup_age = None
    if backup.get("date"):
        try:
            from datetime import date

            backup_age = (clock.club_today() - date.fromisoformat(backup["date"])).days
        except ValueError:
            pass
    with db.connect() as conn:
        warnings = conn.execute(
            "SELECT COUNT(*) FROM activity_events WHERE kind='warning' "
            "AND created_at > datetime('now','-7 days')"
        ).fetchone()[0]
        tokens = conn.execute(
            "SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM usage_log "
            "WHERE created_at > datetime('now','-7 days')"
        ).fetchone()[0]
    meeting = meeting_rules.next_meeting()
    days_out = None
    if meeting.get("date"):
        try:
            from datetime import date

            days_out = (date.fromisoformat(meeting["date"]) - clock.club_today()).days
        except ValueError, TypeError:
            pass
    job_rows = jobs.status()
    overdue_jobs = [row["job_name"] for row in job_rows if row["overdue"]]
    failed_jobs = [
        row["job_name"]
        for row in job_rows
        if row["last_failure"]
        and (not row["last_success"] or row["last_failure"] > row["last_success"])
    ]
    return {
        "backupFile": backup.get("file"),
        "backupAgeDays": backup_age,
        "dbMB": round(db.DB_PATH.stat().st_size / 1e6, 1) if db.DB_PATH.exists() else None,
        "warnings7d": warnings,
        "kTokens7d": int(tokens / 1000),
        "memories": db.count_memories(),
        "reflectionRanAt": (db.get_job_state("reflection") or {}).get("ran_at", "")[:10] or "never",
        "enrichSweep": db.get_job_state("enrichment_sweep") or {},
        "outbox": db.outbox_status_counts(),
        "jobs": {
            "total": len(job_rows),
            "overdue": overdue_jobs,
            "failed": failed_jobs,
            "active": sum(bool(row["lease_owner"]) for row in job_rows),
        },
        "meetingScheduled": bool(meeting.get("meetingId")),
        "bookPicked": bool((meeting.get("book") or {}).get("title")),
        "daysToMeeting": days_out,
        "release": (db.current_release() or {}).get("name") or "unnamed",
    }


def problems(facts: dict) -> list[str]:
    """What needs a look, in plain words. Names jobs, never a member or a warning's text."""
    found = []
    age = facts["backupAgeDays"]
    if age is None:
        found.append("no offsite backup recorded")
    elif age > config.OFFSITE_BACKUP_INTERVAL_DAYS:
        found.append(f"offsite backup is {age} days old")
    if facts["warnings7d"]:
        found.append(f"{facts['warnings7d']} warnings in 7 days (#oliver-log)")
    uncertain = facts["outbox"].get("uncertain", 0)
    dead = facts["outbox"].get("dead", 0)
    if uncertain or dead:
        found.append(f"outbox has {uncertain} uncertain and {dead} dead deliveries")
    if facts["jobs"]["overdue"]:
        found.append("scheduler jobs overdue: " + ", ".join(facts["jobs"]["overdue"]))
    if facts["jobs"]["failed"]:
        found.append("scheduler jobs failing: " + ", ".join(facts["jobs"]["failed"]))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print Oliver's health facts. Read only.")
    parser.add_argument("--json", action="store_true", help="print the facts and problems as JSON")
    args = parser.parse_args(argv)
    if not db.DB_PATH.exists():
        # sqlite3 would create an empty database here; a wrong path must fail instead.
        parser.exit(2, f"no Oliver database at {db.DB_PATH}\n")
    facts = snapshot()
    found = problems(facts)
    if args.json:
        print(json.dumps({"facts": facts, "problems": found}, indent=2))
    else:
        print("\n".join(found) if found else "healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
