"""Weekly health digest: Oliver emails the admin one short status note.

Inverted alarming for the hiatus: warnings in #oliver-log only work if someone reads them, so
instead Oliver WRITES every week (Monday morning, club time) — a missing digest is itself the
alarm. The body is deterministic facts (backup age, warnings, usage, memory, next meeting) with
one composed line of Oliver's voice on top; if the model call fails, the facts still go out.
"""

from __future__ import annotations

import logging

from agent import clock, config, db, identities, jobs, oliver
from agent.club import meeting_rules
from agent.mail import outbound

log = logging.getLogger("oliver.health")

JOB_KEY = "health_digest"


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
            "AND created_at > datetime('now','-7 days')").fetchone()[0]
        recent_warns = [r["title"] for r in conn.execute(
            "SELECT title FROM activity_events WHERE kind='warning' "
            "AND created_at > datetime('now','-7 days') ORDER BY id DESC LIMIT 3")]
        tokens = conn.execute(
            "SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM usage_log "
            "WHERE created_at > datetime('now','-7 days')").fetchone()[0]
    meeting = meeting_rules.next_meeting()
    days_out = None
    if meeting.get("date"):
        try:
            from datetime import date
            days_out = (date.fromisoformat(meeting["date"]) - clock.club_today()).days
        except (ValueError, TypeError):
            pass
    job_rows = jobs.status()
    overdue_jobs = [row["job_name"] for row in job_rows if row["overdue"]]
    failed_jobs = [
        row["job_name"] for row in job_rows
        if row["last_failure"]
        and (not row["last_success"] or row["last_failure"] > row["last_success"])
    ]
    return {
        "backupFile": backup.get("file"), "backupAgeDays": backup_age,
        "dbMB": round(db.DB_PATH.stat().st_size / 1e6, 1) if db.DB_PATH.exists() else None,
        "warnings7d": warnings, "recentWarnings": recent_warns,
        "kTokens7d": int(tokens / 1000),
        "memories": db.count_memories(),
        "reflectionRanAt": (db.get_job_state("reflection") or {}).get("ran_at", "")[:10] or "never",
        "enrichSweep": db.get_job_state("enrichment_sweep") or {},
        "outbox": db.outbox_status_counts(),
        "jobs": {"total": len(job_rows), "overdue": overdue_jobs, "failed": failed_jobs,
                 "active": sum(bool(row["lease_owner"]) for row in job_rows)},
        "nextMeeting": (meeting.get("book") or {}).get("title"),
        "daysToMeeting": days_out,
        "release": (db.current_release() or {}).get("name") or "unnamed",
    }


def digest_email(facts: dict) -> tuple[str, str]:
    today = meeting_rules.friendly_date(clock.club_today_iso())
    delivery_alerts = facts["outbox"].get("uncertain", 0) + facts["outbox"].get("dead", 0)
    job_alerts = len(facts["jobs"]["overdue"]) + len(facts["jobs"]["failed"])
    ok = (facts["warnings7d"] == 0 and not delivery_alerts and not job_alerts
          and (facts["backupAgeDays"] is not None and facts["backupAgeDays"] <= 1))
    opener = oliver.compose(
        "one or two sentences opening your weekly health report to Jamie — your own status, "
        "in your voice, honest about the headline (all quiet, or something needs a look)",
        facts, fallback="All quiet in the engine room this week." if ok
        else "A few things below are worth a look when you have a minute.")
    lines = [
        opener, "",
        f"- **Backup**: {facts['backupFile'] or 'NONE'}"
        + (f" ({facts['backupAgeDays']}d old)" if facts["backupAgeDays"] is not None else "")
        # explicit None check: a 0-day-old backup is the GOOD case, and `0 or 99` would flag it
        + (" ⚠️" if (99 if facts["backupAgeDays"] is None else facts["backupAgeDays"]) > 1 else " ✓"),
        f"- **Warnings (7d)**: {facts['warnings7d']}"
        + (" ✓" if not facts["warnings7d"] else " ⚠️ — " + "; ".join(facts["recentWarnings"])),
        f"- **Outbox**: {facts['outbox'].get('pending', 0)} pending · "
        f"{facts['outbox'].get('retry', 0)} retrying · "
        f"{facts['outbox'].get('uncertain', 0)} uncertain · "
        f"{facts['outbox'].get('dead', 0)} dead"
        + (" ✓" if not delivery_alerts else " ⚠️"),
        f"- **Scheduler jobs**: {facts['jobs']['total']} tracked · "
        f"{len(facts['jobs']['overdue'])} overdue · {len(facts['jobs']['failed'])} failed · "
        f"{facts['jobs']['active']} active"
        + (" ✓" if not job_alerts else " ⚠️"),
        f"- **Database**: {facts['dbMB']} MB · {facts['memories']} active memories · "
        f"last reflection {facts['reflectionRanAt']}",
        f"- **Usage (7d)**: ~{facts['kTokens7d']}k tokens",
        f"- **Next meeting**: {facts['nextMeeting'] or 'none scheduled'}"
        + (f" in {facts['daysToMeeting']} days" if facts["daysToMeeting"] is not None else ""),
        f"- **Running release**: {facts['release']}",
        "",
        "_If this email ever fails to arrive on a Monday, that absence is the alarm — "
        "check the Mac._",
    ]
    subject = f"Oliver health — {today}" + ("" if ok else " ⚠️")
    return subject, "\n".join(lines)


def run(now) -> bool:
    """Weekly gate + send. Called from the hourly scheduler; True when a digest was sent."""
    if not config.HEALTH_DIGEST_ENABLED:
        return False
    if now.weekday() != config.HEALTH_DIGEST_WEEKDAY or now.hour != config.HEALTH_DIGEST_HOUR:
        return False
    week = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
    state = db.get_job_state(JOB_KEY) or {}
    if state.get("week") == week:
        return False  # restart double-fire inside the gate hour
    admin_slug = identities.member_slug_for_user(str(config.ADMIN_USER_ID))
    rec = identities.email_for_member(admin_slug) if admin_slug else None
    if not rec:
        log.warning("health digest: no admin email linked; skipping")
        return False
    subject, body = digest_email(snapshot())
    outbound.send(
        to=[rec["email"]],
        subject=subject,
        body=body,
        idempotency_key=f"email:health:{week}",
        policy="linked_member",
    )
    db.set_job_state(JOB_KEY, {"week": week, "sent_at": db._now()})
    log.info("health digest sent to admin (%s)", week)
    return True
