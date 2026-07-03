"""The `/oliver` slash-command group + the proactive scheduler.

Lives separately from bot.py so the Discord plumbing (client, lifecycle, message
routing, reactions) stays focused. `setup(client)` wires the command group into
the client's tree and stashes a client reference for `run_scheduler` to use
later (it needs `client.get_channel(...)` to post).
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
from datetime import datetime, timedelta

import discord
import requests
from discord.ext import tasks

from agent import (clock, clubdb, config, context as kb, corpus_read, corpus_write, db, oliver,
                   publish, reflection, scheduler, webapp)
from agent.mail import email_jmap, mail_archive, outbound
from agent.club import (meeting_campaign, meeting_emails, meeting_rules,
                        openlibrary, release_notes)

log = logging.getLogger("oliver.commands")

# Stashed by setup(); used by run_scheduler and helpers that need client.
_client: discord.Client | None = None


# ── Publish (rebuild + deploy the site after a data write) ───────────────────
# Coalescing single-flight publisher. Each data write marks the site dirty; one publisher
# task drains it and RE-RUNS if more writes land while it builds — so the last write of a
# burst is always deployed. The task is held in a module global so the event loop can't
# garbage-collect it (un-referenced tasks are only weakly held → can vanish mid-run).
_publisher_task: asyncio.Task | None = None
_publish_dirty = False


def schedule_publish() -> None:
    """Mark the site dirty and ensure a background publisher is running (fast Discord ack)."""
    global _publisher_task, _publish_dirty
    _publish_dirty = True
    if _publisher_task is not None and not _publisher_task.done():
        return  # a publisher is already running; its dirty-recheck will cover this write
    _publisher_task = asyncio.create_task(_drain_publishes())


async def _drain_publishes() -> None:
    global _publish_dirty
    while _publish_dirty:
        _publish_dirty = False
        # publish_site() regenerates the whole corpus from the DB, so any single successful
        # run captures every write committed so far. PublishBusy only happens if another
        # process (a manual `python -m agent.publish`) holds the file lock — retry for that.
        for _ in range(6):
            try:
                await asyncio.to_thread(publish.publish_site)
                break
            except publish.PublishBusy:
                await asyncio.sleep(20)
            except Exception:
                log.exception("background publish failed")
                db.add_activity(
                    "warning", "Site publish failed",
                    "A data write succeeded but rebuilding/deploying the site failed. "
                    "Run `python -m agent.publish` manually, or check the logs.",
                )
                break


# ── Self-healing publish (meeting rollover needs no human) ───────────────────
# The deployed site is built at a moment in time. If a book is added but its deferred publish is
# lost (e.g. the bot restarts before the web app idles out), or a meeting simply rolls over so the
# next book changes, gh-pages goes stale. Each build stamps /next.json with the book it thinks is
# next; this check compares that to the live corpus and republishes on a mismatch — on startup and
# hourly, so rollover is fully automatic.
_NEXT_MARKER_URL = config.SITE_URL + "/next.json"


def _expected_next_book_slug() -> str | None:
    """The earliest still-upcoming book per the live corpus — what a fresh build would stamp into
    /next.json (same rule the site's journey/homepage use). None if there's no upcoming book."""
    upcoming = [b for b in corpus_read.books() if b.get("isUpcoming") and b.get("meetingDate")]
    if not upcoming:
        return None
    upcoming.sort(key=lambda b: b.get("meetingDate") or "")
    return upcoming[0].get("slug")


def _deployed_next_book_slug() -> tuple[bool, str | None]:
    """Read /next.json off the live site. Returns (reachable, nextBookSlug). reachable=False on a
    network error (can't tell — caller skips); a 404 is reachable with slug=None (a pre-marker
    build, i.e. genuinely stale)."""
    try:
        r = requests.get(_NEXT_MARKER_URL, timeout=15)
    except requests.RequestException:
        return (False, None)
    if r.status_code == 404:
        return (True, None)
    if not r.ok:
        return (False, None)
    try:
        return (True, (r.json() or {}).get("nextBookSlug"))
    except ValueError:
        return (True, None)


async def ensure_site_reflects_next_book() -> bool:
    """If the deployed site doesn't show the correct next book, trigger a publish. Returns True if
    it did. Safe to call repeatedly (startup + hourly); skips when a publish is already pending or
    the marker can't be read, so it never thrashes."""
    expected = await asyncio.to_thread(_expected_next_book_slug)
    if expected is None:
        return False  # nothing upcoming to verify
    if _publish_dirty or (_publisher_task is not None and not _publisher_task.done()):
        return False  # a publish is already in flight — it will carry the current state
    reachable, deployed = await asyncio.to_thread(_deployed_next_book_slug)
    if not reachable:
        log.warning("site self-heal: couldn't read %s; skipping this cycle", _NEXT_MARKER_URL)
        return False
    if deployed == expected:
        return False  # site is current
    log.info("site self-heal: deployed next book %r != expected %r — publishing", deployed, expected)
    db.add_activity(
        "site_selfheal", "Auto-publishing to fix a stale site",
        f"The live site's next book is {deployed or '(none — old build)'} but it should be "
        f"“{expected}”. Rebuilding and deploying so the site reflects the current meeting — "
        "no action needed.")
    schedule_publish()
    return True


# ── Admin gate ───────────────────────────────────────────────────────────────
def _is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.id == config.ADMIN_USER_ID


def admin_only(func):
    """Wrap a slash command so non-admins get a quiet ephemeral refusal.

    functools.wraps preserves __wrapped__ so discord.py's `inspect.signature`
    still sees the original parameters when building the slash command schema.
    Apply BELOW the @oliver_cmds.command decorator (so the check runs inside
    the registered handler).
    """
    @functools.wraps(func)
    async def wrapper(interaction: discord.Interaction, *args, **kwargs):
        if interaction.user.id != config.ADMIN_USER_ID:
            await interaction.response.send_message(
                "That's an admin command.", ephemeral=True)
            return
        return await func(interaction, *args, **kwargs)
    return wrapper


# ── The /oliver group ────────────────────────────────────────────────────────
oliver_cmds = discord.app_commands.Group(
    name="oliver", description="Ask Oliver, or help run the R/W Book Club."
)

# Identity/contact management lives under `/oliver contact …` — a nested subcommand group, both to
# group it logically and to stay under Discord's 25-subcommand-per-group ceiling. Admin `link-*`
# (link anyone) and member self-service `add-*`/`remove-*` (your own handles) both live here.
contact_cmds = discord.app_commands.Group(
    name="contact", description="Manage member contact handles — websites, emails, phones.",
    parent=oliver_cmds,
)

# Domain subcommand groups, so `/oliver` reads as a handful of purposes rather than a flat list.
# discord.py nests these under oliver_cmds automatically (parent=), within Discord's 2-level limit:
# `/oliver <group> <subcommand> [options]`.
reading_cmds = discord.app_commands.Group(
    name="reading", description="Your reading progress for the next book.", parent=oliver_cmds)
meeting_cmds = discord.app_commands.Group(
    name="meeting", description="Run the next meeting — roll call, reading check-ins, readiness.",
    parent=oliver_cmds)
timeline_cmds = discord.app_commands.Group(
    name="timeline", description="The club's event timeline — view it or record an event.",
    parent=oliver_cmds)
memory_cmds = discord.app_commands.Group(
    name="memory", description="Oliver's durable memory (admin).", parent=oliver_cmds)
library_cmds = discord.app_commands.Group(
    name="library", description="Club reading data — add books, schedule reads (admin).",
    parent=oliver_cmds)
admin_cmds = discord.app_commands.Group(
    name="admin", description="Operate Oliver — stats, feedback, proposals, scheduler (admin).",
    parent=oliver_cmds)


# ── Autocompletes ────────────────────────────────────────────────────────────
async def book_autocomplete(interaction: discord.Interaction, current: str):
    return [
        discord.app_commands.Choice(name=title[:100], value=slug)
        for title, slug in corpus_read.book_choices(current, limit=25)
    ]


async def member_autocomplete(interaction: discord.Interaction, current: str):
    cur = current.strip().lower()
    out = []
    for m in corpus_read.members():
        name = m.get("name") or ""
        if not cur or cur in name.lower():
            out.append(discord.app_commands.Choice(name=name, value=m.get("slug")))
        if len(out) >= 25:
            break
    return out


def _linked_member_for_user(user_id: int) -> dict | None:
    slug = db.member_slug_for_user(str(user_id))
    return corpus_read.find_member(slug) if slug else None


def _reading_status_text() -> str:
    meeting = meeting_rules.next_meeting()
    book = meeting.get("book") or {}
    title = book.get("title") or "the current book"
    meeting_id = meeting["meetingId"]
    rows = _reading_status_by_member(meeting_id)
    current = sorted(
        [m for m in corpus_read.members() if m.get("isCurrent")],
        key=lambda m: m.get("name") or m["slug"],
    )
    lines = [f"Reading status for **{title}** on {meeting['date']}:"]
    for member in current:
        row = rows.get(member["slug"])
        if not row or (row.get("reading") or "unknown") == "unknown":
            lines.append(f"• {member['name']}: unknown")
            continue
        details = []
        if row.get("reading_progress"):
            details.append(row["reading_progress"])
        if row.get("reading_page") is not None:
            details.append(f"page {row['reading_page']}")
        if row.get("reading_percent") is not None:
            details.append(f"{row['reading_percent']}%")
        suffix = f" — {', '.join(details)}" if details else ""
        lines.append(f"• {member['name']}: {row['reading'].replace('_', ' ')}{suffix}")
    return "\n".join(lines)


def _reading_status_by_member(meeting_id: int | None) -> dict[str, dict]:
    rows = db.meeting_member_status_for_meeting(meeting_id) if meeting_id is not None else []
    return {r["member_slug"]: r for r in rows}


def _attendance_by_member(status: dict) -> dict[str, str]:
    return {r["memberSlug"]: r["status"] for r in status["attendance"]}


# Roll-call email text lives in meeting_rules so the command path and the
# request_roll_call_update tool share one copy (no wording drift).
_days_until_text = meeting_rules.days_until_text
_roll_call_email_body = meeting_rules.roll_call_email_body


def _admin_check_message(interaction: discord.Interaction) -> str | None:
    return None if _is_admin(interaction) else "That's an admin command."


def _club_now() -> datetime:
    return clock.club_now()


def _meeting_datetime(meeting: dict) -> datetime | None:
    """The meeting's local start as a club-tz aware datetime (honors `startTime`). Cadence that's
    "N days before the meeting" is bounded against this, so it honors the meeting's time, not just
    its date, and never fires at the midnight heartbeat."""
    return clock.meeting_start(meeting.get("date"), meeting.get("startTime"))


def _roll_call_message(status: dict | None = None) -> str:
    status = status or meeting_rules.meeting_status()
    meeting = status["meeting"]
    title = (meeting.get("book") or {}).get("title") or "the next meeting"
    return (
        f"📋 **Roll call:** {title} on {meeting['date']}\n"
        "Please tap your attendance below. We need 3 of 5 current members, and the picker has to be there."
    )


async def _roll_call_announcement(status: dict) -> str:
    """Oliver's voiced roll-call announcement; degrades to the template on LLM failure."""
    meeting = status["meeting"]
    title = (meeting.get("book") or {}).get("title") or "the next meeting"
    facts = {
        "what": "you are opening an attendance roll call for the upcoming meeting",
        "book": title,
        "meeting date": meeting["date"],
        "quorum rule": "we need 3 of 5 current members, and the picker has to attend",
        "how to respond": "members tap the attendance buttons directly below your message",
    }
    return await asyncio.to_thread(
        oliver.compose, "roll-call announcement for the club channel",
        facts, fallback=_roll_call_message(status),
    )


async def _roll_call_reminder(status: dict) -> str:
    """Oliver's voiced reminder nudging pending members; degrades to the status template."""
    meeting = status["meeting"]
    title = (meeting.get("book") or {}).get("title") or "the next meeting"
    counts = status["counts"]
    facts = {
        "what": "a reminder nudging members who haven't answered to confirm attendance; "
                "you are re-posting the roll call",
        "book": title,
        "meeting date": meeting["date"],
        "responses so far": (
            f"{counts['yes']} yes, {counts['no']} no, {counts['unsure']} unsure, "
            f"{counts['pending']} pending"
        ),
        "yes responses needed": counts["quorumRequired"],
        "how to respond": "members tap the attendance buttons directly below your message",
    }
    return await asyncio.to_thread(
        oliver.compose, "roll-call reminder for the club channel",
        facts, fallback="🔔 Roll call reminder.\n\n" + meeting_rules.format_status(status),
    )


async def _send_roll_call_email_to_member(member: dict, status: dict) -> dict | None:
    email = db.email_for_member(member["slug"])
    if not email:
        return None
    meeting = status["meeting"]
    title = (meeting.get("book") or {}).get("title") or "the next meeting"
    subject = meeting_rules.roll_call_subject(status)
    timing = _days_until_text(meeting["date"])
    counts = status["counts"]
    picker = ", ".join(meeting.get("pickerNames") or [])
    body = await asyncio.to_thread(
        oliver.compose,
        "roll-call email asking a club member whether they can attend the next meeting",
        {
            "recipient name": member["name"],
            "book": title,
            "meeting date": meeting["date"] + (f" ({timing})" if timing else ""),
            "picker": picker or None,
            "picker rule": "the picker needs to be able to attend" if picker else None,
            "how to respond": "reply yes, no, or unsure and Oliver updates the roll-call tracker",
            "current responses": (
                f"{counts['yes']} yes, {counts['no']} no, {counts['unsure']} unsure, "
                f"{counts['pending']} pending; {counts['quorumRequired']} yes responses needed"
            ),
        },
        fallback=_roll_call_email_body(member["name"], status),
        medium="email",
    )
    meeting_id = meeting["meetingId"]
    member_id = clubdb.lookup_member_id(member["slug"])
    if meeting_id is None or member_id is None:
        return None
    sent_email = await asyncio.to_thread(
        outbound.send,
        to=[email["email"]],
        subject=subject,
        body=body,
    )
    await asyncio.to_thread(db.record_attendance_request, meeting_id, member_id,
                            actor="oliver", surface="email")
    db.add_activity(
        "email_sent",
        "Roll-call email sent",
        f"Member: {member['slug']}\nTo: {email['email']}\nSubject: {subject}",
    )
    return sent_email


async def _send_reading_checkin_email_to_member(member: dict, meeting: dict,
                                                *, note: str | None = None) -> dict | None:
    email = db.email_for_member(member["slug"])
    if not email:
        return None
    title = (meeting.get("book") or {}).get("title") or "the current book"
    subject = f"Reading check-in: {title}"
    timing = _days_until_text(meeting["date"])
    body = await asyncio.to_thread(
        oliver.compose,
        "short reading check-in email asking a club member how far along they are in the book",
        {
            "recipient name": member["name"],
            "book": title,
            "meeting date": meeting["date"] + (f" ({timing})" if timing else ""),
            "how to respond": (
                'reply briefly like "halfway and on track", "page 120, behind", or "finished" '
                "and Oliver updates the tracker"
            ),
            "extra note": note,
        },
        fallback=meeting_rules.reading_checkin_email_body(member["name"], meeting, note=note),
        medium="email",
    )
    meeting_id = meeting["meetingId"]
    member_id = clubdb.lookup_member_id(member["slug"])
    if meeting_id is None or member_id is None:
        return None
    sent = await asyncio.to_thread(
        outbound.send,
        to=[email["email"]],
        subject=subject,
        body=body,
    )
    await asyncio.to_thread(db.record_reading_request, meeting_id, member_id,
                            actor="oliver", surface="email")
    db.add_activity(
        "email_sent",
        "Reading check-in email sent",
        f"Member: {member['slug']}\nTo: {email['email']}\nSubject: {subject}\nEmail ID: {sent.get('emailId')}",
    )
    return sent


# Oliver runs meeting prep once a day, at this local hour, so the per-member evaluation (and its
# decision calls) happen on a daily cadence rather than on every hourly scheduler tick.
MEETING_OUTREACH_HOUR = 9  # America/Chicago
REFLECTION_WEEKDAY = 6     # Sunday
REFLECTION_HOUR = 5        # 5am club time — quiet, far from member-facing outreach


async def _run_meeting_outreach(meeting: dict, status: dict) -> int:
    """Autonomous per-member meeting prep: roll call until attendance is answered, then reading
    check-ins until finished — email only, no admin needed.

    `meeting_campaign.outreach_plan` applies the hard rails (2-week window, the 3-day floor, and the
    ceiling/kickoff that sets `mustReach`); for the discretionary middle cases Oliver decides via
    `oliver.decide_outreach`. Reuses the existing per-member senders, which compose the email and
    record the `attendance_request` / `reading_request` event. Returns the number of emails sent.
    """
    campaign = await asyncio.to_thread(meeting_campaign.snapshot)
    plan = meeting_campaign.outreach_plan(campaign, today=_club_now().date())
    posted = 0
    for cand in plan:
        slug = cand["memberSlug"]
        member = corpus_read.find_member(slug)
        if not member or not db.email_for_member(slug):
            continue
        reach = cand["mustReach"] or await asyncio.to_thread(oliver.decide_outreach, cand)
        if not reach:
            continue
        try:
            if cand["kind"] == "attendance":
                sent = await _send_roll_call_email_to_member(member, status)
            else:
                sent = await _send_reading_checkin_email_to_member(
                    member, meeting, note="Automated reading check-in.")
        except Exception:
            log.exception("meeting outreach (%s) failed for %s", cand["kind"], slug)
            continue
        if sent:
            db.add_activity(
                "meeting_outreach",
                f"Meeting {cand['kind']} outreach sent",
                f"Member: {slug}\nKind: {cand['kind']}\n"
                f"Reason: {'forced' if cand['mustReach'] else 'Oliver chose to reach out'}\n"
                f"Meeting: {meeting['meetingKey']}",
            )
            posted += 1
    return posted


async def _email_roll_call(interaction: discord.Interaction) -> None:
    if not email_jmap.enabled():
        await interaction.response.send_message("Email is not configured.", ephemeral=True)
        return
    status = meeting_rules.meeting_status()
    meeting = status["meeting"]
    current = sorted(
        [m for m in corpus_read.members() if m.get("isCurrent")],
        key=lambda m: m.get("name") or m["slug"],
    )
    attendance = _attendance_by_member(status)
    current = [m for m in current if attendance.get(m["slug"], "pending") == "pending"]
    sent: list[str] = []
    skipped: list[str] = [
        f"{m.get('member')} ({m.get('status')})"
        for m in status["attendance"]
        if m.get("status") != "pending"
    ]
    missing: list[str] = []
    await interaction.response.defer(ephemeral=True)
    for member in current:
        email = db.email_for_member(member["slug"])
        if not email:
            missing.append(member["name"])
            continue
        try:
            await _send_roll_call_email_to_member(member, status)
            sent.append(member["name"])
        except Exception:
            log.exception("roll-call email failed for %s", member["slug"])
            missing.append(f"{member['name']} (send failed)")
    if sent and meeting["meetingId"] is not None and not db.has_open_roll_call(meeting["meetingId"]):
        db.record_group_event(
            meeting["meetingId"],
            "roll_call_opened",
            actor="oliver",
            detail={
                "channel_id": str(interaction.channel_id) if interaction.channel_id else None,
                "opened_by": f"email:{interaction.user.id}",
            },
        )
    lines = [f"Sent roll-call email to {len(sent)} pending member(s): {', '.join(sent) or 'none'}."]
    if skipped:
        lines.append("Skipped already-confirmed: " + ", ".join(skipped) + ".")
    if missing:
        lines.append("Not emailed: " + ", ".join(missing) + ".")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


class AttendanceView(discord.ui.View):
    """Simple persistent button view for the current meeting roll call."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="I'll be there", style=discord.ButtonStyle.success,
                       custom_id="oliver:attendance:yes")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await record_attendance_response(interaction, "yes")

    @discord.ui.button(label="I can't make it", style=discord.ButtonStyle.danger,
                       custom_id="oliver:attendance:no")
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await record_attendance_response(interaction, "no")

    @discord.ui.button(label="Unsure", style=discord.ButtonStyle.secondary,
                       custom_id="oliver:attendance:unsure")
    async def unsure(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await record_attendance_response(interaction, "unsure")


async def record_attendance_response(interaction: discord.Interaction, status: str) -> None:
    member = _linked_member_for_user(interaction.user.id)
    if not member:
        await interaction.response.send_message(
            "I don't have your Discord account linked to a current club member yet.",
            ephemeral=True)
        return
    meeting = meeting_rules.next_meeting()
    meeting_id = meeting["meetingId"]
    member_id = clubdb.lookup_member_id(member["slug"])
    if meeting_id is None or member_id is None:
        await interaction.response.send_message(
            "There's no scheduled meeting to record against yet.", ephemeral=True)
        return
    roll_call = db.current_roll_call(meeting_id)
    if roll_call and roll_call.get("status") == "closed":
        await interaction.response.send_message("That roll call is closed.", ephemeral=True)
        return
    db.record_attendance_report(
        meeting_id,
        member_id,
        status,
        surface="discord",
        updated_by=str(interaction.user.id),
    )
    db.add_activity(
        "roll_call_update",
        "Roll-call response recorded",
        f"Member: {member['slug']}\nStatus: {status}\nSource: Discord button\nMeeting: {meeting['meetingKey']}",
    )
    status_words = {"yes": "attending", "no": "not attending", "unsure": "unsure"}
    summary = meeting_rules.meeting_status(meeting["meetingId"])
    await interaction.response.send_message(
        f"Recorded you as {status_words[status]}.\n\n{meeting_rules.format_status(summary)}",
        ephemeral=True)


# ── Subcommands ──────────────────────────────────────────────────────────────
@oliver_cmds.command(name="ping", description="Check that Oliver is awake.")
async def oliver_ping(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("🟢 Oliver is awake.", ephemeral=True)


@oliver_cmds.command(name="webapp", description="Get a private link to manage your club info on the web.")
async def oliver_webapp(interaction: discord.Interaction) -> None:
    member = _linked_member_for_user(interaction.user.id)
    if not member:
        await interaction.response.send_message(_LINK_FIRST, ephemeral=True)
        return
    member_id = clubdb.lookup_member_id(member["slug"])
    if member_id is None:
        await interaction.response.send_message(
            "I couldn't resolve your member record — ask an admin to check your link.", ephemeral=True)
        return
    await webapp.ensure_running()  # spin the server up on demand (it idles off when unused)
    token = await asyncio.to_thread(
        webapp.mint_token, member_id, is_admin=_is_admin(interaction))
    url = f"{config.WEBAPP_BASE_URL}/webapp?t={token}"
    # Angle brackets suppress Discord's link unfurl, so its preview bot won't pre-fetch (and burn)
    # the single-use token before you tap it. The server also ignores preview bots as a backstop.
    await interaction.response.send_message(
        f"🔧 Your private link (good for ~15 minutes, just for you):\n<{url}>\n"
        "_Blank page? The editor sleeps after ~15 min idle (or a restart) — just run "
        "`/oliver webapp` again to wake it._", ephemeral=True)


@admin_cmds.command(name="stats", description="Report corpus stats (admin).")
@admin_only
async def oliver_stats(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        f"Corpus holds {kb.book_count()} books. Model: {oliver.MODEL}.", ephemeral=True,
    )


@admin_cmds.command(name="release-notes",
                     description="Draft & send release notes from recent changes (admin).")
@discord.app_commands.describe(
    days="Look back this many days (1-90). Ignored if 'since' is set.",
    since="Or scope to changes since this git commit (hash or ref).",
    to="Where to send — yourself (default) or the club mailing list")
# Default scope (no days, no since): everything since the last release notes — or the last
# 7 days if none has ever been sent. A list send records HEAD as the next baseline.
@discord.app_commands.choices(to=[
    discord.app_commands.Choice(name="me", value="me"),
    discord.app_commands.Choice(name="list", value="list"),
])
@admin_only
async def release_notes_cmd(interaction: discord.Interaction, days: int | None = None,
                            since: str | None = None,
                            to: discord.app_commands.Choice[str] | None = None) -> None:
    if since:
        resolved = release_notes.resolve_commit(since)
        if not resolved:
            await interaction.response.send_message(
                f"I couldn't find a commit matching `{since}`.", ephemeral=True)
            return
        days_arg, since_arg, scope = None, resolved, f"since `{resolved}`"
    elif days is None and (baseline := db.last_release_notes_commit()) \
            and release_notes.resolve_commit(baseline):
        # No explicit scope → pick up where the last release notes left off, so every change
        # since then is covered exactly once.
        days_arg, since_arg, scope = None, baseline, f"since the last release notes (`{baseline}`)"
    else:
        d = days if days is not None else 7
        if not 1 <= d <= 90:
            await interaction.response.send_message("Pick a window between 1 and 90 days.", ephemeral=True)
            return
        days_arg, since_arg, scope = d, None, f"the last {d} days"
    if not email_jmap.enabled():
        await interaction.response.send_message(
            "Email isn't configured (no FASTMAIL_JMAP_TOKEN), so I can't send release notes.",
            ephemeral=True)
        return

    target = to.value if to else "me"
    await interaction.response.defer(ephemeral=True)
    try:
        email = await asyncio.to_thread(
            release_notes.release_notes_email, days=days_arg, since_commit=since_arg)
    except Exception:
        log.exception("release-notes generation failed")
        db.add_activity("warning", "Release notes failed",
                        f"Scope: {scope}\nGenerating the release-notes email raised an exception.")
        await interaction.followup.send("Something went wrong drafting the release notes.", ephemeral=True)
        return
    if email is None:
        await interaction.followup.send(f"No changes {scope} — nothing to announce.", ephemeral=True)
        return

    try:
        name = email.get("release_name") or None
        christened = f"\nChristened: *{name}*" if name else ""
        if target == "list":
            await _send_club_email(email["subject"], email["body"])
            db.add_activity("release_notes_sent", "Release notes sent",
                            f"Scope: {scope}\nTo: club mailing list\nSubject: {email['subject']}"
                            + (f"\nRelease name: {name}" if name else ""))
            # Mark this release in the club timeline and store HEAD as the baseline the next
            # release-notes scopes from (so it auto-covers everything shipped since). A named
            # list send is what christens the release — the name becomes current_release().
            head = release_notes.head_commit()
            if head:
                db.record_release_notes_sent(head, scope=scope, subject=email["subject"],
                                             window=email.get("window"), release_name=name)
            await interaction.followup.send(
                f"📣 Sent release notes ({scope}) to the club list and mirrored to the main "
                f"channel.\nSubject: *{email['subject']}*{christened}", ephemeral=True)
        else:
            slug = db.member_slug_for_user(str(interaction.user.id))
            rec = db.email_for_member(slug) if slug else None
            if not rec:
                await interaction.followup.send(
                    "You don't have a linked email address, so I can only send this to the list "
                    "(`to:list`) — or link an email first.", ephemeral=True)
                return
            sent = await asyncio.to_thread(
                outbound.send, to=[rec["email"]], subject=email["subject"], body=email["body"])
            db.add_activity("release_notes_sent", "Release notes sent",
                            f"Scope: {scope}\nTo: {rec['email']} (admin)\nSubject: {email['subject']}\n"
                            f"Email ID: {sent.get('emailId')}")
            await interaction.followup.send(
                f"📝 Emailed the release-notes draft ({scope}) to `{rec['email']}` "
                f"(`{sent.get('emailId')}`).\nSubject: *{email['subject']}*"
                + (f"\nDraft release name: *{name}* (christened only on a list send)" if name else "")
                + "\nReview it, then re-run with `to:list` to send it to the club.", ephemeral=True)
    except Exception:
        log.exception("release-notes send failed")
        db.add_activity("warning", "Release notes send failed",
                        f"Days: {days}\nTo: {target}\nThe draft was generated but sending failed.")
        await interaction.followup.send("I drafted the notes but couldn't send the email.", ephemeral=True)


@admin_cmds.command(name="postscript",
                    description="Draft the after-meeting 'Postscript' digest and email it to you (admin test).")
@admin_only
async def postscript_cmd(interaction: discord.Interaction) -> None:
    if not email_jmap.enabled():
        await interaction.response.send_message(
            "Email isn't configured (no FASTMAIL_JMAP_TOKEN), so I can't draft Postscript.",
            ephemeral=True)
        return
    slug = db.member_slug_for_user(str(interaction.user.id))
    rec = db.email_for_member(slug) if slug else None
    if not rec:
        await interaction.response.send_message(
            "You don't have a linked email address — link one first (`/oliver contact link-email`).",
            ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        email = await asyncio.to_thread(meeting_emails.postscript_email)
    except Exception:
        log.exception("postscript generation failed")
        db.add_activity("warning", "Postscript draft failed",
                        "Generating the Postscript digest raised an exception.")
        await interaction.followup.send("Something went wrong drafting Postscript.", ephemeral=True)
        return
    try:
        # Self-only test send — to the admin's own linked email, never the club list. Bypasses the
        # flag/window/dedup and records no group event, so it can be re-run freely while iterating.
        sent = await asyncio.to_thread(
            outbound.send, to=[rec["email"]], subject=email["subject"], body=email["body"])
    except Exception:
        log.exception("postscript send failed")
        await interaction.followup.send("I drafted Postscript but couldn't send the email.", ephemeral=True)
        return
    db.add_activity("club_email_sent", "Postscript test draft sent",
                    f"To: {rec['email']} (admin test)\nItems offered: {len(email['offered'])}\n"
                    f"Email ID: {sent.get('emailId')}")
    await interaction.followup.send(
        f"📝 Emailed a Postscript draft to `{rec['email']}` (`{sent.get('emailId')}`) — test only, "
        "not the club. Check the items are real, then set `CLUB_POSTSCRIPT_ENABLED=1` for the real "
        "~1-week-after-meeting send.", ephemeral=True)


@contact_cmds.command(name="link-member", description="Link a Discord user to a club member (admin).")
@discord.app_commands.describe(member="Club member", user="Discord user")
@discord.app_commands.autocomplete(member=member_autocomplete)
@admin_only
async def link_member_cmd(interaction: discord.Interaction, member: str, user: discord.User) -> None:
    m = corpus_read.find_member(member)
    if not m:
        await interaction.response.send_message("I couldn't find that member.", ephemeral=True)
        return
    db.link_member_identity(str(user.id), m["slug"], linked_by=str(interaction.user.id))
    await interaction.response.send_message(
        f"Linked {user.mention} to {m['name']} (`{m['slug']}`).", ephemeral=True)


@contact_cmds.command(name="link-email", description="Link an email address to a club member (admin).")
@discord.app_commands.describe(member="Club member", email="Email address")
@discord.app_commands.autocomplete(member=member_autocomplete)
@admin_only
async def link_email_cmd(interaction: discord.Interaction, member: str, email: str) -> None:
    m = corpus_read.find_member(member)
    if not m:
        await interaction.response.send_message("I couldn't find that member.", ephemeral=True)
        return
    try:
        db.link_member_email(email, m["slug"], linked_by=str(interaction.user.id))
    except ValueError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return
    # Re-attribute any archived mail from this address to the member (history catches up).
    rescored = await asyncio.to_thread(mail_archive.reattribute_archive, email)
    extra = f" Re-attributed {rescored} archived message(s)." if rescored else ""
    await interaction.response.send_message(
        f"Linked `{email.strip().lower()}` to {m['name']} (`{m['slug']}`).{extra}", ephemeral=True)


@contact_cmds.command(name="link-sms", description="Link a phone number to a club member (admin).")
@discord.app_commands.describe(member="Club member", number="Phone number")
@discord.app_commands.autocomplete(member=member_autocomplete)
@admin_only
async def link_sms_cmd(interaction: discord.Interaction, member: str, number: str) -> None:
    m = corpus_read.find_member(member)
    if not m:
        await interaction.response.send_message("I couldn't find that member.", ephemeral=True)
        return
    try:
        db.link_member_sms(number, m["slug"], linked_by=str(interaction.user.id))
    except ValueError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return
    await interaction.response.send_message(
        f"Linked `{number.strip()}` to {m['name']} (`{m['slug']}`).", ephemeral=True)


# Members manage their own contact handles, lists, and ratings/reviews in the web app now
# (/oliver webapp). _LINK_FIRST is still used by /oliver webapp for unlinked callers; the admin
# link-* commands above and the underlying writers (db.link_member_*, agent/club/lists.py) stay.
_LINK_FIRST = ("I can only do that for linked club members — ask an admin to run "
               "`/oliver contact link-member` to connect your Discord account first.")


@admin_cmds.command(name="reattribute-mail",
                     description="Re-resolve archived mail senders to members (admin).")
@admin_only
async def reattribute_mail_cmd(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    changed = await asyncio.to_thread(mail_archive.reattribute_archive)
    await interaction.followup.send(
        f"Re-attributed {changed} archived message(s) to their current member links."
        if changed else "Archive attribution already up to date — nothing changed.",
        ephemeral=True)


@contact_cmds.command(name="list", description="Show Discord, email, and SMS identity links (admin).")
@admin_only
async def identities_cmd(interaction: discord.Interaction) -> None:
    rows = db.list_member_identities()
    email_rows = db.list_member_emails()
    sms_rows = db.list_member_sms()
    website_rows = db.list_member_websites()
    members = corpus_read.members()
    if not rows and not email_rows and not sms_rows and not website_rows:
        missing = ", ".join(sorted(m["name"] for m in members if m.get("isCurrent")))
        await interaction.response.send_message(
            f"No Discord identities linked yet. Current members not linked: {missing}.",
            ephemeral=True)
        return
    names = {m["slug"]: m.get("name") for m in members}
    linked_slugs = {r["member_slug"] for r in rows}
    email_linked_slugs = {r["member_slug"] for r in email_rows}
    lines = ["**Linked identities:**"]
    for r in rows:
        lines.append(f"• {names.get(r['member_slug'], r['member_slug'])}: Discord <@{r['discord_user_id']}>")
    for r in email_rows:
        lines.append(f"• {names.get(r['member_slug'], r['member_slug'])}: email `{r['email']}`")
    for r in sms_rows:
        lines.append(f"• {names.get(r['member_slug'], r['member_slug'])}: sms `{r['number']}`")
    for r in website_rows:
        lines.append(f"• {names.get(r['member_slug'], r['member_slug'])}: website `{r['url']}`")
    missing = [m["name"] for m in members if m.get("isCurrent") and m["slug"] not in linked_slugs]
    if missing:
        lines.append("\n**Current members without Discord:** " + ", ".join(sorted(missing)))
    missing_email = [
        m["name"] for m in members if m.get("isCurrent") and m["slug"] not in email_linked_slugs
    ]
    if missing_email:
        lines.append("\n**Current members without email:** " + ", ".join(sorted(missing_email)))
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@oliver_cmds.command(name="whoami", description="Check which club member Oliver has linked you to.")
async def whoami_cmd(interaction: discord.Interaction) -> None:
    member = _linked_member_for_user(interaction.user.id)
    if member:
        await interaction.response.send_message(
            f"I have you linked as {member['name']}.", ephemeral=True)
        return
    await interaction.response.send_message(
        "I don't have your Discord account linked to a club member yet.", ephemeral=True)


@reading_cmds.command(name="status", description="Show or update your reading progress for the next book.")
@discord.app_commands.describe(
    status="Optional status: not_started, started, on_track, behind, finished, paused",
    progress="Optional short note, e.g. 'chapter 6' or 'halfway'",
    page="Optional page number",
    percent="Optional percent complete",
)
async def reading_status_cmd(interaction: discord.Interaction, status: str | None = None,
                             progress: str | None = None, page: int | None = None,
                             percent: int | None = None) -> None:
    if not any(v is not None for v in (status, progress, page, percent)):
        await interaction.response.send_message(_reading_status_text(), ephemeral=True)
        return
    member = _linked_member_for_user(interaction.user.id)
    if not member:
        await interaction.response.send_message(
            "I can only update reading status from linked club members.", ephemeral=True)
        return
    normalized = (status or "started").strip().lower().replace("-", "_").replace(" ", "_")
    try:
        meeting = meeting_rules.next_meeting()
        meeting_id = meeting["meetingId"]
        member_id = clubdb.lookup_member_id(member["slug"])
        if meeting_id is None or member_id is None:
            await interaction.response.send_message(
                "There's no scheduled meeting to record reading status against yet.", ephemeral=True)
            return
        db.record_reading_report(
            meeting_id,
            member_id,
            normalized,
            progress=progress,
            page=page,
            percent=percent,
            surface="discord",
            updated_by=str(interaction.user.id),
        )
        db.add_activity(
            "reading_update",
            "Reading status recorded",
            f"Member: {member['slug']}\nStatus: {normalized}\nProgress: {progress or '-'}\nSource: Discord command\nMeeting: {meeting['meetingKey']}",
        )
    except ValueError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return
    await interaction.response.send_message(_reading_status_text(), ephemeral=True)


@meeting_cmds.command(name="check-in", description="Email a reading-status check-in to a member (admin).")
@discord.app_commands.describe(member="Club member", note="Optional extra sentence")
@discord.app_commands.autocomplete(member=member_autocomplete)
@admin_only
async def reading_checkin_cmd(interaction: discord.Interaction, member: str,
                              note: str | None = None) -> None:
    m = corpus_read.find_member(member)
    if not m:
        await interaction.response.send_message("I couldn't find that member.", ephemeral=True)
        return
    email = db.email_for_member(m["slug"])
    if not email:
        await interaction.response.send_message(
            f"{m['name']} does not have a linked email address.", ephemeral=True)
        return
    if not email_jmap.enabled():
        await interaction.response.send_message("Email is not configured.", ephemeral=True)
        return
    meeting = meeting_rules.next_meeting()
    book = meeting.get("book") or {}
    title = book.get("title") or "the current book"
    meeting_id = meeting["meetingId"]
    member_id = clubdb.lookup_member_id(m["slug"])
    existing = (db.meeting_member_status(meeting_id, member_id)
                if (meeting_id is not None and member_id is not None) else None)
    if existing and existing["reading"] == "finished":
        db.add_activity(
            "reading_checkin_skipped",
            "Reading check-in skipped",
            f"Member: {m['slug']}\nReason: already finished\nBook: {title}",
        )
        await interaction.response.send_message(
            f"{m['name']} is already marked finished for {title}; not sending a check-in.",
            ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        sent = await _send_reading_checkin_email_to_member(m, meeting, note=note)
    except Exception:
        log.exception("reading-checkin email failed")
        await interaction.followup.send("I couldn't send that email.", ephemeral=True)
        return
    await interaction.followup.send(
        f"Sent reading check-in to {m['name']} at `{email['email']}` (`{sent.get('emailId')}`).",
        ephemeral=True)


@library_cmds.command(name="add-book", description="Add a book to the corpus (admin) — fetches metadata from Open Library.")
@discord.app_commands.describe(title="Book title", isbn="ISBN (optional, more precise)")
@admin_only
async def oliver_add_book(interaction: discord.Interaction, title: str, isbn: str | None = None) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        meta = await asyncio.to_thread(openlibrary.lookup, title, isbn)
        if not meta or not meta.get("title"):
            await interaction.followup.send(
                f"Couldn't find “{title}” on Open Library — you can add it by hand in corpus/data/books/.",
                ephemeral=True)
            return
        res = await asyncio.to_thread(corpus_write.write_book, meta)
    except Exception:
        log.exception("add-book failed")
        await interaction.followup.send("Something went wrong adding that book.", ephemeral=True)
        return
    schedule_publish()  # rebuild + deploy the site in the background
    authors = ", ".join(res["authors"]) or "unknown author"
    cover = "with cover" if res["hasCover"] else "no cover found"
    verb = "Updated" if res["updated"] else "Added"
    ack = await asyncio.to_thread(
        oliver.compose,
        "brief acknowledgement that an admin just added a book to the club corpus",
        {"action": verb.lower(), "book": res["title"], "authors": authors, "cover": cover},
        fallback=f"📗 {verb} **{res['title']}** by {authors} ({cover}).",
    )
    # Keep the exact file path + next step deterministic (don't let the LLM mangle it).
    await interaction.followup.send(
        f"{ack}\n\nEdit details in the web app (/oliver webapp → Books), then schedule it there under Meetings.",
        ephemeral=True,
    )


@timeline_cmds.command(name="log", description="Record a club timeline event (admin).")
@discord.app_commands.describe(
    category="Event category",
    kind="Event kind, e.g. dinner, book_picked, member_away, member_milestone, meeting_held",
    date="When it happened (YYYY-MM-DD)", summary="One factual sentence",
    member="Optional member the event is about (omit for club-wide)")
@discord.app_commands.choices(category=[
    discord.app_commands.Choice(name=c, value=c)
    for c in ("meeting", "selection", "social", "member_life", "club", "reading")
])
@discord.app_commands.autocomplete(member=member_autocomplete)
@admin_only
async def oliver_log_event(interaction: discord.Interaction,
                           category: discord.app_commands.Choice[str], kind: str,
                           date: str, summary: str, member: str | None = None) -> None:
    cat = category.value
    allowed = db.CHRONICLE_KINDS.get(cat) or ()
    if kind not in allowed:
        await interaction.response.send_message(
            f"`{kind}` isn't a valid kind for **{cat}**. Allowed: {', '.join(allowed)}.",
            ephemeral=True)
        return
    member_slug = None
    member_id = None
    if member:
        m = corpus_read.find_member(member)
        if not m:
            await interaction.response.send_message("I couldn't find that member.", ephemeral=True)
            return
        member_slug = m["slug"]
        member_id = clubdb.lookup_member_id(member_slug)
    eid = await asyncio.to_thread(
        db.record_event, actor="admin", surface="discord", kind=kind, category=cat,
        member_id=member_id,
        detail={"summary": summary, "members": [member_slug] if member_slug else []},
        occurred_at=(date or "")[:10] or None,
    )
    who = f" for {member_slug}" if member_slug else " (club-wide)"
    await interaction.response.send_message(
        f"🗓️ Logged **{cat}/{kind}** on {date}{who} (#{eid}).", ephemeral=True)


@timeline_cmds.command(name="show", description="Show recent club timeline events.")
@discord.app_commands.describe(member="Optional member to scope to", category="Optional category")
@discord.app_commands.autocomplete(member=member_autocomplete)
@discord.app_commands.choices(category=[
    discord.app_commands.Choice(name=c, value=c)
    for c in ("meeting", "selection", "social", "member_life", "club", "reading", "meeting_ops")
])
async def oliver_timeline(interaction: discord.Interaction, member: str | None = None,
                          category: discord.app_commands.Choice[str] | None = None) -> None:
    member_id = None
    if member:
        m = corpus_read.find_member(member)
        if not m:
            await interaction.response.send_message("I couldn't find that member.", ephemeral=True)
            return
        member_id = clubdb.lookup_member_id(m["slug"])
    rows = await asyncio.to_thread(
        db.timeline, category=(category.value if category else None), member_id=member_id, limit=20)
    if not rows:
        await interaction.response.send_message("No timeline events recorded yet.", ephemeral=True)
        return
    lines = ["**Club timeline** (most recent):"]
    for r in rows:
        detail = r.get("detail") or ""
        try:
            summary = json.loads(detail).get("summary") if detail.startswith("{") else detail
        except (json.JSONDecodeError, AttributeError):
            summary = detail
        who = f" [{r['member_slug']}]" if r.get("member_slug") else ""
        lines.append(f"• {(r.get('occurred_at') or '')[:10]} ({r['kind']}){who}: {summary}")
    await interaction.response.send_message("\n".join(lines)[:config.MAX_DISCORD_LEN], ephemeral=True)


@admin_cmds.command(name="feedback", description="Recent 👍/👎 feedback on Oliver's replies (admin).")
@admin_only
async def oliver_feedback(interaction: discord.Interaction) -> None:
    stats = await asyncio.to_thread(db.feedback_stats)
    if not stats["total"]:
        await interaction.response.send_message(
            "No feedback yet — members can 👍/👎 any of my replies and I'll log it.",
            ephemeral=True)
        return
    lines = [f"📊 **Feedback to date:** {stats['up']} 👍 · {stats['down']} 👎  ({stats['total']} total)"]
    if stats["recent_down"]:
        lines.append("\n**Recent 👎:**")
        for r in stats["recent_down"]:
            q = (r.get("question") or "(no question recorded)")[:100]
            lines.append(f"• {r['user_name']}: \"{q}\"")
    if stats["recent_up"]:
        lines.append("\n**Recent 👍:**")
        for r in stats["recent_up"]:
            q = (r.get("question") or "(no question recorded)")[:100]
            lines.append(f"• {r['user_name']}: \"{q}\"")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@meeting_cmds.command(name="roll-call", description="Start, check, remind, or close meeting roll call.")
@discord.app_commands.describe(action="What to do with the current meeting roll call")
@discord.app_commands.choices(action=[
    discord.app_commands.Choice(name="status", value="status"),
    discord.app_commands.Choice(name="start", value="start"),
    discord.app_commands.Choice(name="remind", value="remind"),
    discord.app_commands.Choice(name="email", value="email"),
    discord.app_commands.Choice(name="close", value="close"),
])
async def roll_call_cmd(interaction: discord.Interaction,
                        action: discord.app_commands.Choice[str]) -> None:
    act = action.value
    if act in {"start", "remind", "email", "close"}:
        msg = _admin_check_message(interaction)
        if msg:
            await interaction.response.send_message(msg, ephemeral=True)
            return

    meeting = meeting_rules.next_meeting()
    if act == "status":
        await interaction.response.send_message(
            meeting_rules.format_status(meeting_rules.meeting_status(meeting["meetingId"])),
            ephemeral=True)
        return

    if act == "close":
        if meeting["meetingId"] is not None and db.has_open_roll_call(meeting["meetingId"]):
            db.record_group_event(meeting["meetingId"], "roll_call_closed", actor="admin")
        await interaction.response.send_message(
            meeting_rules.format_status(meeting_rules.meeting_status(meeting["meetingId"])),
            ephemeral=True)
        return

    if act == "email":
        await _email_roll_call(interaction)
        return

    # Both start and remind post the roll call with attendance buttons, in Oliver's
    # voice — defer past the 3s ack window before composing.
    status = meeting_rules.meeting_status(meeting["meetingId"])
    await interaction.response.defer()
    text = await (_roll_call_announcement(status) if act == "start"
                  else _roll_call_reminder(status))
    sent = await interaction.followup.send(text, view=AttendanceView())
    try:
        if meeting["meetingId"] is not None:
            db.record_group_event(
                meeting["meetingId"],
                "roll_call_opened",
                actor="oliver",
                detail={
                    "channel_id": str(sent.channel.id),
                    "message_id": str(sent.id),
                    "opened_by": str(interaction.user.id),
                },
            )
    except discord.HTTPException:
        log.exception("Failed to record roll-call message")


@meeting_cmds.command(name="dashboard", description="Show the next meeting readiness dashboard (admin).")
@admin_only
async def meeting_dashboard_cmd(interaction: discord.Interaction) -> None:
    text = await asyncio.to_thread(meeting_campaign.format_dashboard)
    await interaction.response.send_message(text[:config.MAX_DISCORD_LEN], ephemeral=True)


@admin_cmds.command(name="proposals", description="Show Oliver's pending action proposals (admin).")
@admin_only
async def proposals_cmd(interaction: discord.Interaction) -> None:
    rows = await asyncio.to_thread(db.list_proposals, limit=10)
    if not rows:
        await interaction.response.send_message("No pending proposals.", ephemeral=True)
        return
    lines = ["**Pending proposals:**"]
    for r in rows:
        lines.append(f"• `{r['id']}` [{r['kind']}] **{r['title']}** — {r['body'][:180]}")
    await interaction.response.send_message("\n".join(lines)[:config.MAX_DISCORD_LEN], ephemeral=True)


@admin_cmds.command(name="resolve", description="Accept or dismiss an Oliver proposal (admin).")
@discord.app_commands.describe(proposal_id="Proposal id from /oliver proposals",
                               decision="Accept or dismiss")
@discord.app_commands.choices(decision=[
    discord.app_commands.Choice(name="accept", value="accepted"),
    discord.app_commands.Choice(name="dismiss", value="dismissed"),
])
@admin_only
async def resolve_proposal_cmd(interaction: discord.Interaction, proposal_id: int,
                               decision: discord.app_commands.Choice[str]) -> None:
    ok = await asyncio.to_thread(
        db.resolve_proposal, proposal_id, decision.value, resolved_by=str(interaction.user.id)
    )
    await interaction.response.send_message(
        f"Proposal {decision.name}ed." if ok else "No pending proposal with that id.",
        ephemeral=True)


@memory_cmds.command(name="search", description="Search Oliver's durable memories (admin).")
@discord.app_commands.describe(subject="Optional member slug or topic", query="Optional text search")
@admin_only
async def memories_cmd(interaction: discord.Interaction, subject: str | None = None,
                       query: str | None = None) -> None:
    rows = await asyncio.to_thread(db.get_memories, subject=subject, query=query, limit=10)
    if not rows:
        await interaction.response.send_message("No matching memories.", ephemeral=True)
        return
    lines = ["**Oliver memories:**"]
    for r in rows:
        scope = r.get("scope") or "general"
        subj = f"/{r['subject']}" if r.get("subject") else ""
        src = f" · source: {r['source']}" if r.get("source") else ""
        lines.append(f"• `{r['id']}` [{scope}{subj}] {r['note']}{src}")
    await interaction.response.send_message("\n".join(lines)[:config.MAX_DISCORD_LEN], ephemeral=True)


@memory_cmds.command(name="edit", description="Edit one of Oliver's memories (admin).")
@discord.app_commands.describe(memory_id="Memory id from /oliver memory search", note="Replacement note")
@admin_only
async def edit_memory_cmd(interaction: discord.Interaction, memory_id: int, note: str) -> None:
    ok = await asyncio.to_thread(db.update_memory, memory_id, note)
    await interaction.response.send_message(
        "Updated memory." if ok else "No active memory with that id.", ephemeral=True)


@memory_cmds.command(name="forget", description="Delete one of Oliver's memories (admin).")
@discord.app_commands.describe(memory_id="Memory id from /oliver memory search")
@admin_only
async def forget_cmd(interaction: discord.Interaction, memory_id: int) -> None:
    ok = await asyncio.to_thread(db.delete_memory, memory_id)
    await interaction.response.send_message(
        "Forgot that memory." if ok else "No active memory with that id.", ephemeral=True)


@admin_cmds.command(name="tick", description="Run the proactive scheduler now (admin).")
@admin_only
async def oliver_tick(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    n = await run_scheduler()
    await interaction.followup.send(
        f"Posted {n} notification(s)." if n else "Nothing due right now.", ephemeral=True)


# ── Scheduler ────────────────────────────────────────────────────────────────
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


async def _send_club_email(subject: str, body: str) -> None:
    """Send a club-wide cadence email to the mailing list and mirror it to Discord.

    This is the charter's "approved cadence path" — a direct send to the whole list,
    distinct from the gated send_email tool. The signature is finalized once so the
    emailed and Discord-mirrored copies match.
    """
    final = outbound.finalize(body)
    await asyncio.to_thread(
        outbound.send,
        to=[config.BOOK_CLUB_MAILING_LIST_ADDRESS],
        subject=subject,
        body=final,
        sign=False,  # already finalized above
    )
    main = _client.get_channel(config.MAIN_CHANNEL_ID) if config.MAIN_CHANNEL_ID else None
    if main is not None:
        for chunk in _chunk(final, config.MAX_DISCORD_LEN):
            await main.send(chunk)


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
    await _send_club_email(email["subject"], email["body"])
    db.record_group_event(meeting_id, meeting_emails.POSTSCRIPT_KIND, actor="oliver",
                          surface="email", detail=json.dumps({"featured": email["offered"]}))
    db.add_activity("club_email_sent", "Postscript sent to the mailing list",
                    f"Meeting: {recent.get('slug')}\nItems offered: {len(email['offered'])}")
    return 1


async def run_scheduler() -> int:
    """Post anything due to its target channel; returns the count posted.

    Two sources:
    - Corpus-derived notifications (scheduler.due_notifications) go to MAIN_CHANNEL_ID.
    - User-set reminders (db.due_reminders) fire in the channel they were set in
      (falling back to MAIN_CHANNEL_ID if none was recorded).
    """
    if _client is None:
        return 0
    posted = 0
    now = _club_now()

    # 0. Self-heal the deployed site if it doesn't reflect the current next book (a lost deferred
    # publish, or a meeting rollover). Runs on startup (first tick) + hourly — no human needed.
    try:
        await ensure_site_reflects_next_book()
    except Exception:
        log.exception("site self-heal check failed")

    # 0b. Weekly reflective memory (Sunday early morning): distill the week's conversations into
    # durable member memories. Internal + audited in #oliver-log. The hourly loop ticks once inside
    # the gate hour; if a restart double-fires it, the advanced watermark makes the second run a
    # quiet no-op — and a failed run keeps its watermark so it retries next week.
    if (config.OLIVER_REFLECTION_ENABLED and now.weekday() == REFLECTION_WEEKDAY
            and now.hour == REFLECTION_HOUR):
        try:
            await asyncio.to_thread(reflection.run)
        except Exception:
            log.exception("weekly reflection failed")

    # 1. Corpus-derived notifications → main channel.
    main = _client.get_channel(config.MAIN_CHANNEL_ID) if config.MAIN_CHANNEL_ID else None
    if config.MAIN_CHANNEL_ID and main is None:
        log.warning("DISCORD_MAIN_CHANNEL_ID %s not found", config.MAIN_CHANNEL_ID)
    if main is not None:
        due = await asyncio.to_thread(scheduler.due_notifications, now, db.sent_keys())
        for note in due:
            msg = await asyncio.to_thread(
                oliver.compose, note.kind, note.facts, fallback=note.fallback
            )
            await main.send(msg)
            db.mark_sent(note.key)
            posted += 1

        status = await asyncio.to_thread(meeting_rules.meeting_status)
        meeting = status["meeting"]
        try:
            meeting_date = datetime.fromisoformat(meeting["date"])
        except ValueError:
            meeting_date = None
        if meeting_date:
            days = (meeting_date.date() - now.date()).days
            # Time-aware meeting start (honors start_time): cadence bounded "N days before" uses
            # this so it fires at the meeting's hour N days prior, not on the midnight heartbeat.
            meeting_dt = _meeting_datetime(meeting)
            meeting_id = meeting["meetingId"]

            # Autonomous per-member meeting prep (roll call → reading check-ins): email only,
            # evaluated once a day at MEETING_OUTREACH_HOUR. Oliver paces itself off the event log,
            # so no admin command is needed to collect attendance + reading for a meeting.
            if (email_jmap.enabled() and meeting_id is not None
                    and 0 <= days <= meeting_campaign.OUTREACH_START_DAYS
                    and now.hour == MEETING_OUTREACH_HOUR):
                posted += await _run_meeting_outreach(meeting, status)

            status = await asyncio.to_thread(meeting_rules.meeting_status)
            if (0 <= days <= 3 and status["recommendation"] != "ready"
                    and meeting_id is not None
                    and not db.has_group_event(meeting_id, "attendance_alert_sent")):
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
                await main.send(alert)
                db.record_group_event(meeting_id, "attendance_alert_sent", actor="oliver",
                                      surface="discord")
                db.add_activity(
                    "attendance_alert",
                    "Attendance alert posted",
                    f"Meeting: {meeting['meetingKey']}\nRecommendation: {status['recommendation']}",
                )
                posted += 1

            # Club-wide cadence emails (OFF unless CLUB_EMAIL_CADENCE_ENABLED): the 1-week
            # reminder and the 2-day discussion-topics email, each once per meeting. The "N days
            # before" bound honors the meeting's TIME (meeting_dt - N days <= now <= meeting_dt),
            # so these go out at the meeting's hour N days prior — never at the midnight heartbeat.
            if (config.CLUB_EMAIL_CADENCE_ENABLED and email_jmap.enabled()
                    and meeting_id is not None and meeting_dt is not None):
                if (meeting_dt - timedelta(days=7) <= now <= meeting_dt
                        and not db.has_group_event(meeting_id, "week_reminder_sent")):
                    email = await asyncio.to_thread(meeting_emails.week_reminder, meeting, status)
                    await _send_club_email(email["subject"], email["body"])
                    db.record_group_event(meeting_id, "week_reminder_sent", actor="oliver",
                                          surface="email")
                    db.add_activity("club_email_sent", "1-week reminder sent to the mailing list",
                                    f"Meeting: {meeting['meetingKey']}")
                    posted += 1
                if (meeting_dt - timedelta(days=2) <= now <= meeting_dt
                        and not db.has_group_event(meeting_id, "briefing_sent")):
                    email = await asyncio.to_thread(meeting_emails.topic_email, meeting)
                    await _send_club_email(email["subject"], email["body"])
                    db.record_group_event(meeting_id, "briefing_sent", actor="oliver",
                                          surface="email")
                    db.add_activity("club_email_sent", "2-day topic email sent to the mailing list",
                                    f"Meeting: {meeting['meetingKey']}")
                    posted += 1

        # Postscript — the ~1-week-AFTER-meeting digest (OFF unless CLUB_POSTSCRIPT_ENABLED),
        # keyed on the most-recent PAST meeting (independent of the upcoming one above).
        if config.CLUB_POSTSCRIPT_ENABLED and email_jmap.enabled():
            posted += await _maybe_send_postscript(now)

    # 2. User-set reminders → their original channel (or main as fallback).
    reminders = await asyncio.to_thread(db.due_reminders)
    for r in reminders:
        target_id = int(r["channel_id"]) if r.get("channel_id") else config.MAIN_CHANNEL_ID
        target = _client.get_channel(target_id) if target_id else None
        if target is None:
            log.warning("reminder %s: channel %s not found, skipping", r["id"], target_id)
            db.mark_reminder_fired(r["id"])  # don't keep retrying a missing channel
            continue
        msg = f"⏰ Reminder: {r['text']}"
        if r.get("created_by"):
            msg += f"\n_(set by {r['created_by']})_"
        try:
            await target.send(msg)
            db.mark_reminder_fired(r["id"])
            db.add_activity(
                "reminder_sent",
                "Reminder sent",
                f"Reminder ID: {r['id']}\nChannel: {target_id}\nText: {r['text'][:500]}",
            )
            posted += 1
        except discord.HTTPException:
            log.exception("Failed to post reminder %s; will retry next tick", r["id"])

    return posted


# Hourly, not daily: corpus-derived notifications are deduped by key
# (notifications_sent), so re-checking costs nothing, and user-set reminders
# from db.due_reminders fire within an hour of their due time instead of
# waiting up to a full day for the next tick.
@tasks.loop(hours=1)
async def scheduler_loop() -> None:
    try:
        n = await run_scheduler()
        if n:
            log.info("scheduler posted %d notification(s)", n)
    except Exception:
        log.exception("scheduler loop error")


# ── Wiring ───────────────────────────────────────────────────────────────────
def setup(client: discord.Client) -> None:
    """Attach the command group to the client's tree and stash the client reference."""
    global _client
    _client = client
    client.add_view(AttendanceView())
    client.tree.add_command(oliver_cmds)


def start_scheduler() -> None:
    """Kick off the hourly scheduler loop. Call from on_ready once the gateway is up."""
    if config.MAIN_CHANNEL_ID and not scheduler_loop.is_running():
        scheduler_loop.start()
