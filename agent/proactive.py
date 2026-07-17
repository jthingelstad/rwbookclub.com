"""Proactive runtime coordinator for durable jobs, club cadence, and reminders."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

import discord
from discord.ext import tasks

from agent import (
    backup,
    clock,
    clubdb,
    config,
    db,
    delivery,
    health,
    jobs,
    oliver,
    outbox,
    publishing,
    reflection,
    scheduler,
)
from agent.club import (
    meeting_campaign,
    meeting_emails,
    meeting_rules,
    outreach,
    review_drive,
)
from agent.enrich import loop as enrich_loop
from agent.mail import email_jmap, outbound

log = logging.getLogger("oliver.proactive")

_client: discord.Client | None = None
REFLECTION_WEEKDAY = 6
REFLECTION_HOUR = 5


def _chunk(text: str, limit: int) -> list[str]:
    """Split text into <=limit pieces, breaking at newlines where possible."""
    out, remaining = [], text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        out.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        out.append(remaining)
    return out


async def _send_discord(channel, content: str, *, idempotency_key: str) -> dict:
    """Persist one proactive Discord post before crossing Discord's API boundary."""
    payload = {"channel_id": str(channel.id), "content": content}
    row = outbox.enqueue(kind="discord", payload=payload, idempotency_key=idempotency_key)
    return await delivery.deliver_discord_row(row, channel)


async def send_club_email(subject: str, body: str, *, idempotency_key: str | None = None) -> None:
    """Send a club-wide cadence email to the mailing list and mirror it to Discord.

    This is the charter's "approved cadence path" — a direct send to the whole list,
    distinct from the gated send_email tool. The signature is finalized once so the
    emailed and Discord-mirrored copies match.
    """
    final = outbound.finalize(body)
    base_key = outbox.stable_key(
        "club-email",
        {"subject": subject, "body": final},
        explicit=idempotency_key,
    )
    await asyncio.to_thread(
        outbound.send,
        to=[config.BOOK_CLUB_MAILING_LIST_ADDRESS],
        subject=subject,
        body=final,
        sign=False,  # already finalized above
        idempotency_key=f"{base_key}:email",
        policy="cadence",
    )
    main = _client.get_channel(config.MAIN_CHANNEL_ID) if config.MAIN_CHANNEL_ID else None
    if main is not None:
        for index, chunk in enumerate(_chunk(final, config.MAX_DISCORD_LEN)):
            await _send_discord(
                main,
                chunk,
                idempotency_key=f"{base_key}:discord:{index}",
            )


async def _maybe_send_postscript(now: datetime) -> int:
    """Send Postscript (the after-meeting digest) once, ~1 week after the most-recent past meeting.
    The window is bounded on both ends so enabling the feature never retroactively fires for old
    meetings; the group event dedups it per meeting and records the offered slugs for rotation."""
    recent = meeting_emails._most_recent_read_book()
    if not recent or not recent.get("meetingDate"):
        return 0
    meeting_id = clubdb.meeting_id_for_book_slug(recent.get("slug"))
    if meeting_id is None or db.has_group_event(meeting_id, meeting_emails.POSTSCRIPT_KIND):
        return 0
    start = clock.meeting_start(recent.get("meetingDate"), recent.get("meetingStartTime"))
    if not (start + timedelta(days=7) <= now <= start + timedelta(days=10)):
        return 0
    email = await asyncio.to_thread(meeting_emails.postscript_email, recent)
    await send_club_email(
        email["subject"],
        email["body"],
        idempotency_key=f"club-email:postscript:{meeting_id}",
    )
    db.record_group_event(
        meeting_id,
        meeting_emails.POSTSCRIPT_KIND,
        actor="oliver",
        surface="email",
        detail=json.dumps({"featured": email["offered"]}),
    )
    db.add_activity(
        "club_email_sent",
        "Postscript sent to the mailing list",
        f"Meeting: {recent.get('slug')}\nItems offered: {len(email['offered'])}",
    )
    return 1


async def _scheduled_job(name: str, work) -> object | None:
    """Run one scheduler component under its own observable renewable lease."""
    try:
        result = await jobs.run(name, work)
    except Exception:
        log.exception("scheduled job %s failed", name)
        return None
    return result.value if result.executed else None


async def _run_maintenance_jobs(now: datetime) -> int:
    """Run non-channel scheduler components; return recovered deliveries."""
    delivered = await _scheduled_job("outbox_delivery", lambda: delivery.drain(_client))
    await _scheduled_job("site_reconcile", publishing.reconcile)

    async def backup_job() -> int:
        return int(bool(await asyncio.to_thread(backup.run)))

    await _scheduled_job("offsite_backup", backup_job)

    async def enrichment_job() -> int:
        if (
            not config.ENRICH_SWEEP_ENABLED
            or (db.get_job_state("enrichment_sweep") or {}).get("date") == clock.club_today_iso()
        ):
            return 0
        summary = await asyncio.to_thread(enrich_loop.run_pending, limit=config.ENRICH_SWEEP_LIMIT)
        db.set_job_state(
            "enrichment_sweep",
            {
                "date": clock.club_today_iso(),
                **{key: value for key, value in summary.items() if key != "exhausted"},
                "exhausted": len(summary["exhausted"]),
            },
        )
        if summary["enriched"] or summary["retried"]:
            log.info("enrichment sweep: %s", summary)
            publishing.schedule()
        return summary["enriched"] + summary["retried"]

    await _scheduled_job("enrichment_sweep", enrichment_job)
    await _scheduled_job("review_drive", lambda: asyncio.to_thread(review_drive.run, now))
    await _scheduled_job("health_digest", lambda: asyncio.to_thread(health.run, now))

    async def reflection_job() -> int:
        if not (
            config.OLIVER_REFLECTION_ENABLED
            and now.weekday() == REFLECTION_WEEKDAY
            and now.hour == REFLECTION_HOUR
        ):
            return 0
        summary = await asyncio.to_thread(reflection.run)
        return int(summary.get("members") or 0)

    await _scheduled_job("reflection", reflection_job)
    return int(delivered or 0)


async def _post_due_notifications(main, now: datetime) -> int:
    posted = 0
    due = await asyncio.to_thread(scheduler.due_notifications, now, db.sent_keys())
    for note in due:
        msg = await asyncio.to_thread(oliver.compose, note.kind, note.facts, fallback=note.fallback)
        await _send_discord(main, msg, idempotency_key=f"discord:notification:{note.key}")
        db.mark_sent(note.key)
        posted += 1
    return posted


async def _maybe_run_meeting_outreach(meeting: dict, status: dict, now: datetime, days: int) -> int:
    meeting_id = meeting["meetingId"]
    if (
        email_jmap.enabled()
        and meeting_id is not None
        and 0 <= days <= meeting_campaign.OUTREACH_START_DAYS
        and now.hour == outreach.MEETING_OUTREACH_HOUR
    ):
        return await outreach.run(meeting, status)
    return 0


async def _maybe_post_attendance_alert(main, meeting: dict, now: datetime, days: int) -> int:
    status = await asyncio.to_thread(meeting_rules.meeting_status)
    meeting_id = meeting["meetingId"]
    if (
        not 0 <= days <= 3
        or status["recommendation"] == "ready"
        or meeting_id is None
        or db.has_group_event(meeting_id, "attendance_alert_sent")
    ):
        return 0
    counts = status["counts"]
    meeting_book = (meeting.get("book") or {}).get("title") or "the next book"
    alert = await asyncio.to_thread(
        oliver.compose,
        "attendance alert nudging the club to confirm before the meeting",
        {
            "concern": "attendance for the upcoming meeting may fall short of quorum",
            "book": meeting_book,
            "meeting date": meeting["date"],
            "days away": days,
            "responses so far": (
                f"{counts['yes']} yes, {counts['no']} no, "
                f"{counts['unsure']} unsure, {counts['pending']} pending"
            ),
            "yes responses needed": counts["quorumRequired"],
        },
        fallback="⚠️ Attendance may need attention.\n\n" + meeting_rules.format_status(status),
    )
    await _send_discord(main, alert, idempotency_key=f"discord:attendance-alert:{meeting_id}")
    db.record_group_event(meeting_id, "attendance_alert_sent", actor="oliver", surface="discord")
    db.add_activity(
        "attendance_alert",
        "Attendance alert posted",
        f"Meeting: {meeting['meetingKey']}\nRecommendation: {status['recommendation']}",
    )
    return 1


async def _maybe_send_club_cadence(
    meeting: dict, status: dict, meeting_dt: datetime | None, now: datetime
) -> int:
    meeting_id = meeting["meetingId"]
    if (
        not config.CLUB_EMAIL_CADENCE_ENABLED
        or not email_jmap.enabled()
        or meeting_id is None
        or meeting_dt is None
    ):
        return 0
    posted = 0
    if meeting_dt - timedelta(days=7) <= now <= meeting_dt and not db.has_group_event(
        meeting_id, "week_reminder_sent"
    ):
        email = await asyncio.to_thread(meeting_emails.week_reminder, meeting, status)
        await send_club_email(
            email["subject"],
            email["body"],
            idempotency_key=f"club-email:week-reminder:{meeting_id}",
        )
        db.record_group_event(meeting_id, "week_reminder_sent", actor="oliver", surface="email")
        db.add_activity(
            "club_email_sent",
            "1-week reminder sent to the mailing list",
            f"Meeting: {meeting['meetingKey']}",
        )
        posted += 1
    if meeting_dt - timedelta(days=2) <= now <= meeting_dt and not db.has_group_event(
        meeting_id, "briefing_sent"
    ):
        email = await asyncio.to_thread(meeting_emails.topic_email, meeting)
        await send_club_email(
            email["subject"],
            email["body"],
            idempotency_key=f"club-email:briefing:{meeting_id}",
        )
        db.record_group_event(meeting_id, "briefing_sent", actor="oliver", surface="email")
        db.add_activity(
            "club_email_sent",
            "2-day topic email sent to the mailing list",
            f"Meeting: {meeting['meetingKey']}",
        )
        posted += 1
    return posted


async def _run_meeting_jobs(main, now: datetime) -> int:
    status = await asyncio.to_thread(meeting_rules.meeting_status)
    meeting = status["meeting"]
    try:
        meeting_date = datetime.fromisoformat(meeting["date"])
    except ValueError:
        meeting_date = None
    posted = 0
    if meeting_date:
        days = (meeting_date.date() - now.date()).days
        meeting_dt = clock.meeting_start(meeting.get("date"), meeting.get("startTime"))
        posted += await _maybe_run_meeting_outreach(meeting, status, now, days)
        posted += await _maybe_post_attendance_alert(main, meeting, now, days)
        status = await asyncio.to_thread(meeting_rules.meeting_status)
        posted += await _maybe_send_club_cadence(meeting, status, meeting_dt, now)
    if config.CLUB_POSTSCRIPT_ENABLED and email_jmap.enabled():
        posted += await _maybe_send_postscript(now)
    return posted


async def _post_due_reminders() -> int:
    posted = 0
    reminders = await asyncio.to_thread(db.due_reminders)
    for reminder in reminders:
        target_id = (
            int(reminder["channel_id"]) if reminder.get("channel_id") else config.MAIN_CHANNEL_ID
        )
        target = _client.get_channel(target_id) if target_id else None
        if target is None:
            log.warning("reminder %s: channel %s not found, skipping", reminder["id"], target_id)
            db.mark_reminder_fired(reminder["id"])
            continue
        msg = f"⏰ Reminder: {reminder['text']}"
        if reminder.get("created_by"):
            msg += f"\n_(set by {reminder['created_by']})_"
        try:
            await _send_discord(target, msg, idempotency_key=f"discord:reminder:{reminder['id']}")
            db.mark_reminder_fired(reminder["id"])
            db.add_activity(
                "reminder_sent",
                "Reminder sent",
                f"Reminder ID: {reminder['id']}\nChannel: {target_id}\n"
                f"Text: {reminder['text'][:500]}",
            )
            posted += 1
        except discord.HTTPException, outbox.OutboxError:
            log.exception("Failed to post reminder %s; delivery was not confirmed", reminder["id"])
    return posted


async def _run_unleased() -> int:
    """Post anything due to its target channel; returns the count posted.

    Two sources:
    - Corpus-derived notifications (scheduler.due_notifications) go to MAIN_CHANNEL_ID.
    - User-set reminders (db.due_reminders) fire in the channel they were set in
      (falling back to MAIN_CHANNEL_ID if none was recorded).
    """
    if _client is None:
        return 0
    now = clock.club_now()
    posted = await _run_maintenance_jobs(now)

    # 1. Corpus-derived notifications → main channel.
    main = _client.get_channel(config.MAIN_CHANNEL_ID) if config.MAIN_CHANNEL_ID else None
    if config.MAIN_CHANNEL_ID and main is None:
        log.warning("DISCORD_MAIN_CHANNEL_ID %s not found", config.MAIN_CHANNEL_ID)
    if main is not None:
        posted += await _post_due_notifications(main, now)

        posted += await _run_meeting_jobs(main, now)

    return posted + await _post_due_reminders()


async def run() -> int:
    """Run one scheduler tick under a top-level lease; overlapping/manual ticks no-op safely."""
    result = await jobs.run("scheduler_tick", _run_unleased)
    return int(result.value or 0) if result.executed else 0


# Hourly, not daily: corpus-derived notifications are deduped by key
# (notifications_sent), so re-checking costs nothing, and user-set reminders
# from db.due_reminders fire within an hour of their due time instead of
# waiting up to a full day for the next tick.
@tasks.loop(hours=1)
async def scheduler_loop() -> None:
    try:
        n = await run()
        if n:
            log.info("scheduler posted %d notification(s)", n)
    except Exception:
        log.exception("scheduler loop error")


def configure(client: discord.Client) -> None:
    """Bind the Discord client used for proactive channel delivery."""
    global _client
    _client = client


def start() -> None:
    """Start the hourly loop after the Discord gateway is ready."""
    if config.MAIN_CHANNEL_ID and not scheduler_loop.is_running():
        scheduler_loop.start()
