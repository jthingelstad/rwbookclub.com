"""The `/oliver` slash-command group + the proactive scheduler.

Lives separately from bot.py so the Discord plumbing (client, lifecycle, message
routing, reactions) stays focused. `setup(client)` wires the command group into
the client's tree and stashes a client reference for `run_scheduler` to use
later (it needs `client.get_channel(...)` to post).
"""

from __future__ import annotations

import asyncio
import functools
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord.ext import tasks

from agent import (config, context as kb, corpus_read, corpus_write, db, oliver,
                   scheduler)
from agent.mail import email_jmap, email_tracking
from agent.club import meeting_campaign, meeting_rules, openlibrary, reviews

log = logging.getLogger("oliver.commands")

# Stashed by setup(); used by run_scheduler and helpers that need client.
_client: discord.Client | None = None


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
    rows = {r["member_slug"]: r for r in db.reading_status_for_meeting(meeting["meetingKey"])}
    current = sorted(
        [m for m in corpus_read.members() if m.get("isCurrent")],
        key=lambda m: m.get("name") or m["slug"],
    )
    lines = [f"Reading status for **{title}** on {meeting['date']}:"]
    for member in current:
        row = rows.get(member["slug"])
        if not row:
            lines.append(f"• {member['name']}: unknown")
            continue
        details = []
        if row.get("progress"):
            details.append(row["progress"])
        if row.get("page") is not None:
            details.append(f"page {row['page']}")
        if row.get("percent") is not None:
            details.append(f"{row['percent']}%")
        suffix = f" — {', '.join(details)}" if details else ""
        lines.append(f"• {member['name']}: {row['status'].replace('_', ' ')}{suffix}")
    return "\n".join(lines)


def _reading_status_by_member(meeting_key: str) -> dict[str, dict]:
    return {r["member_slug"]: r for r in db.reading_status_for_meeting(meeting_key)}


def _attendance_by_member(status: dict) -> dict[str, str]:
    return {r["memberSlug"]: r["status"] for r in status["attendance"]}


def _days_until_text(meeting_date: str) -> str:
    try:
        days = (date.fromisoformat(meeting_date) - date.today()).days
    except ValueError:
        return ""
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days > 1:
        return f"in {days} days"
    return f"{abs(days)} days ago"


def _admin_check_message(interaction: discord.Interaction) -> str | None:
    return None if _is_admin(interaction) else "That's an admin command."


def _club_now() -> datetime:
    try:
        return datetime.now(ZoneInfo(config.CLUB_TIMEZONE))
    except ZoneInfoNotFoundError:
        return datetime.now().astimezone()


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


def _roll_call_email_body(member_name: str, status: dict) -> str:
    meeting = status["meeting"]
    title = (meeting.get("book") or {}).get("title") or "the next meeting"
    timing = _days_until_text(meeting["date"])
    meeting_when = f"{meeting['date']}" + (f" ({timing})" if timing else "")
    picker = ", ".join(meeting.get("pickerNames") or [])
    picker_line = f"\n\n{picker} picked this one, and the picker needs to be able to attend." if picker else ""
    return (
        f"Hi {member_name},\n\n"
        f"Roll call for {title}: the meeting is {meeting_when}.\n\n"
        "Can you make it? Reply with yes, no, or unsure and I'll update the roll-call tracker."
        f"{picker_line}\n\n"
        f"Current status: {status['counts']['yes']} yes, {status['counts']['no']} no, "
        f"{status['counts']['unsure']} unsure, {status['counts']['pending']} pending. "
        f"We need {status['counts']['quorumRequired']} yes responses.\n\n"
        "Oliver"
    )


def _reading_checkin_body(member_name: str, meeting: dict, *, note: str | None = None) -> str:
    book = meeting.get("book") or {}
    title = book.get("title") or "the current book"
    timing = _days_until_text(meeting["date"])
    meeting_when = f"{meeting['date']}" + (f" ({timing})" if timing else "")
    extra = f"\n\n{note.strip()}" if note else ""
    return (
        f"Hi {member_name},\n\n"
        f"Quick reading check-in for {title}. The meeting is {meeting_when}. "
        "Where are you in the book, and do you feel on track?\n\n"
        "Reply with something short like \"halfway and on track\", "
        "\"page 120, behind\", or \"finished\" and I'll update the tracker."
        f"{extra}\n\nOliver"
    )


async def _send_roll_call_email_to_member(member: dict, status: dict) -> dict | None:
    email = db.email_for_member(member["slug"])
    if not email:
        return None
    meeting = status["meeting"]
    title = (meeting.get("book") or {}).get("title") or "the next meeting"
    subject = f"Roll call: {title} on {meeting['date']}"
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
    contact_id = None
    try:
        contact_id, html_body, tracking_token = email_tracking.prepare_outbound(
            text=body,
            meeting_key=meeting["meetingKey"],
            member_slug=member["slug"],
            kind="roll_call",
            subject=subject,
        )
        sent_email = await asyncio.to_thread(
            email_jmap.send_email,
            to=[email["email"]],
            subject=subject,
            body=body,
            html_body=html_body,
        )
        email_tracking.mark_outbound_sent(contact_id, tracking_token, sent_email.get("emailId"))
        db.add_activity(
            "email_sent",
            "Roll-call email sent",
            f"Member: {member['slug']}\nTo: {email['email']}\nSubject: {subject}",
        )
        return sent_email
    except Exception:
        if contact_id is not None:
            email_tracking.mark_outbound_failed(contact_id)
        raise


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
        fallback=_reading_checkin_body(member["name"], meeting, note=note),
        medium="email",
    )
    contact_id = None
    try:
        contact_id, html_body, tracking_token = email_tracking.prepare_outbound(
            text=body,
            meeting_key=meeting["meetingKey"],
            member_slug=member["slug"],
            kind="reading_checkin",
            subject=subject,
        )
        sent = await asyncio.to_thread(
            email_jmap.send_email,
            to=[email["email"]],
            subject=subject,
            body=body,
            html_body=html_body,
        )
        email_tracking.mark_outbound_sent(contact_id, tracking_token, sent.get("emailId"))
        db.add_activity(
            "email_sent",
            "Reading check-in email sent",
            f"Member: {member['slug']}\nTo: {email['email']}\nSubject: {subject}\nEmail ID: {sent.get('emailId')}",
        )
        return sent
    except Exception:
        if contact_id is not None:
            email_tracking.mark_outbound_failed(contact_id)
        raise


def _should_start_scheduled_roll_call(status: dict, roll_call: dict | None) -> bool:
    """Whether the scheduler should post a fresh Discord roll call."""
    if status.get("recommendation") == "ready":
        return False
    if roll_call and roll_call.get("status") == "open" and roll_call.get("message_id"):
        return False
    return True


async def _email_roll_call(interaction: discord.Interaction) -> None:
    if not email_jmap.enabled():
        await interaction.response.send_message("Email is not configured.", ephemeral=True)
        return
    status = meeting_rules.meeting_status()
    meeting = status["meeting"]
    title = (meeting.get("book") or {}).get("title") or "the next meeting"
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
    if sent:
        db.upsert_roll_call(
            meeting_key=meeting["meetingKey"],
            channel_id=str(interaction.channel_id) if interaction.channel_id else None,
            opened_by=f"email:{interaction.user.id}",
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
    roll_call = db.get_roll_call(meeting["meetingKey"])
    if roll_call and roll_call.get("status") == "closed":
        await interaction.response.send_message("That roll call is closed.", ephemeral=True)
        return
    db.upsert_roll_call(
        meeting_key=meeting["meetingKey"],
        channel_id=str(interaction.channel_id) if interaction.channel_id else None,
        opened_by="button",
    )
    db.set_attendance(
        meeting_key=meeting["meetingKey"],
        member_slug=member["slug"],
        status=status,
        updated_by_user_id=str(interaction.user.id),
        source="button",
    )
    db.add_activity(
        "roll_call_update",
        "Roll-call response recorded",
        f"Member: {member['slug']}\nStatus: {status}\nSource: Discord button\nMeeting: {meeting['meetingKey']}",
    )
    status_words = {"yes": "attending", "no": "not attending", "unsure": "unsure"}
    summary = meeting_rules.meeting_status(meeting["meetingKey"])
    await interaction.response.send_message(
        f"Recorded you as {status_words[status]}.\n\n{meeting_rules.format_status(summary)}",
        ephemeral=True)


# ── Modals ───────────────────────────────────────────────────────────────────
class ReviewModal(discord.ui.Modal):
    """The /oliver review form — five inputs, one submit, written to the Git corpus."""

    def __init__(self, slug: str, title: str, member_slug: str,
                 existing: dict | None = None) -> None:
        super().__init__(title=f"Review: {title}"[:45])
        self.slug = slug
        self.member_slug = member_slug
        existing = existing or {}
        rating_default = "DNF" if existing.get("dnf") else (
            str(existing["rating"]) if existing.get("rating") else None)
        self.rating = discord.ui.TextInput(
            label="Rating (1–5, or DNF)", required=False, max_length=12, default=rating_default)
        self.review = discord.ui.TextInput(
            label="Your review", style=discord.TextStyle.paragraph, required=False,
            max_length=2000, default=existing.get("review") or None)
        self.recommend = discord.ui.TextInput(
            label="Would you recommend it? (yes/no)", required=False, max_length=5,
            default=("yes" if existing.get("wouldRecommend") else None))
        self.discussion = discord.ui.TextInput(
            label="Discussion quality (1–5, optional)", required=False, max_length=3,
            default=(str(existing["discussionQuality"]) if existing.get("discussionQuality") else None))
        self.quote = discord.ui.TextInput(
            label="Favorite quote (optional)", style=discord.TextStyle.paragraph,
            required=False, max_length=1000, default=existing.get("favoriteQuote") or None)
        for item in (self.rating, self.review, self.recommend, self.discussion, self.quote):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)  # git write may take a few seconds
        try:
            res = await asyncio.to_thread(
                reviews.write_review, self.slug, self.member_slug,
                rating=self.rating.value, review=self.review.value,
                recommend=self.recommend.value, discussion=self.discussion.value,
                quote=self.quote.value,
            )
        except reviews.ReviewError as e:
            await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
            return
        except Exception:
            log.exception("Failed to write review")
            await interaction.followup.send(
                "Sorry — I couldn't save that just now. Try again in a moment.", ephemeral=True)
            return
        verb = "Updated" if res["updated"] else "Logged"
        score = "DNF" if res["dnf"] else (f"{res['rating']}/5" if res["rating"] else "your notes")
        ack = await asyncio.to_thread(
            oliver.compose,
            "brief acknowledgement that a member just logged their book review",
            {
                "action": verb.lower(),
                "book": res["book"],
                "rating": score,
                "note": "the review will appear on the club website shortly",
            },
            fallback=f"📚 {verb} your review of *{res['book']}* ({score}) — it'll be live on the site shortly.",
        )
        await interaction.followup.send(ack, ephemeral=True)


# ── Subcommands ──────────────────────────────────────────────────────────────
@oliver_cmds.command(name="ping", description="Check that Oliver is awake.")
async def oliver_ping(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("🟢 Oliver is awake.", ephemeral=True)


@oliver_cmds.command(name="stats", description="Report corpus stats (admin).")
@admin_only
async def oliver_stats(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        f"Corpus holds {kb.book_count()} books. Model: {oliver.MODEL}.", ephemeral=True,
    )


@oliver_cmds.command(name="link-member", description="Link a Discord user to a club member (admin).")
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


@oliver_cmds.command(name="link-email", description="Link an email address to a club member (admin).")
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
    await interaction.response.send_message(
        f"Linked `{email.strip().lower()}` to {m['name']} (`{m['slug']}`).", ephemeral=True)


@oliver_cmds.command(name="identities", description="Show Discord and email identity links (admin).")
@admin_only
async def identities_cmd(interaction: discord.Interaction) -> None:
    rows = db.list_member_identities()
    email_rows = db.list_member_emails()
    members = corpus_read.members()
    if not rows and not email_rows:
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


@oliver_cmds.command(name="reading-status", description="Show or update your reading progress for the next book.")
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
        db.set_reading_status(
            meeting_key=meeting["meetingKey"],
            member_slug=member["slug"],
            status=normalized,
            progress=progress,
            page=page,
            percent=percent,
            source="discord",
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


@oliver_cmds.command(name="reading-checkin", description="Email a reading-status check-in to a member (admin).")
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
    existing = db.reading_status_for_member(meeting["meetingKey"], m["slug"])
    if existing and existing["status"] == "finished":
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


@oliver_cmds.command(name="review", description="Log your review of a book the club has read.")
@discord.app_commands.describe(book="The book you're reviewing")
@discord.app_commands.autocomplete(book=book_autocomplete)
async def review_cmd(interaction: discord.Interaction, book: str) -> None:
    member = _linked_member_for_user(interaction.user.id)
    if not member:
        await interaction.response.send_message(
            "I can only log reviews from linked club members — ask an admin to run `/oliver link-member`.",
            ephemeral=True)
        return
    b = corpus_read.find_book(book)
    if not b:
        await interaction.response.send_message(
            "I couldn't find that book — pick one from the suggestions as you type.", ephemeral=True)
        return
    # Prefill the form if this member already reviewed this book.
    existing: dict = {}
    rp = corpus_read.DATA_DIR / "reviews" / f"{b['slug']}--{member['slug']}.md"
    if rp.exists():
        data, body = corpus_read.parse_frontmatter(rp.read_text())
        existing = {**(data or {}), "review": body}
    await interaction.response.send_modal(ReviewModal(b["slug"], b["title"], member["slug"], existing))


@oliver_cmds.command(name="add-book", description="Add a book to the corpus (admin) — fetches metadata from Open Library.")
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
        f"{ack}\n\nEdit `corpus/data/books/{res['slug']}.json` if anything's off, then `/oliver schedule` it.",
        ephemeral=True,
    )


@oliver_cmds.command(name="schedule", description="Schedule the next read — book + date + picker (admin).")
@discord.app_commands.describe(book="The book", date="Meeting date (YYYY-MM-DD)", picker="Who picked it")
@discord.app_commands.autocomplete(book=book_autocomplete, picker=member_autocomplete)
@admin_only
async def oliver_schedule(interaction: discord.Interaction, book: str, date: str, picker: str) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        res = await asyncio.to_thread(corpus_write.schedule_meeting, book, date, picker)
    except corpus_write.WriteError as e:
        await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
        return
    except Exception:
        log.exception("schedule failed")
        await interaction.followup.send("Something went wrong scheduling that.", ephemeral=True)
        return
    ack = await asyncio.to_thread(
        oliver.compose,
        "brief acknowledgement that an admin just scheduled the next club read",
        {"book": res["book"], "meeting date": res["date"], "picker": res["picker"]},
        fallback=f"🗓️ Scheduled **{res['book']}** for {res['date']}, picked by {res['picker']}.",
    )
    await interaction.followup.send(ack, ephemeral=True)


@oliver_cmds.command(name="feedback", description="Recent 👍/👎 feedback on Oliver's replies (admin).")
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


@oliver_cmds.command(name="roll-call", description="Start, check, remind, or close meeting roll call.")
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
            meeting_rules.format_status(meeting_rules.meeting_status(meeting["meetingKey"])),
            ephemeral=True)
        return

    if act == "close":
        db.close_roll_call(meeting["meetingKey"])
        await interaction.response.send_message(
            meeting_rules.format_status(meeting_rules.meeting_status(meeting["meetingKey"])),
            ephemeral=True)
        return

    if act == "email":
        await _email_roll_call(interaction)
        return

    # Both start and remind post the roll call with attendance buttons, in Oliver's
    # voice — defer past the 3s ack window before composing.
    status = meeting_rules.meeting_status(meeting["meetingKey"])
    await interaction.response.defer()
    text = await (_roll_call_announcement(status) if act == "start"
                  else _roll_call_reminder(status))
    sent = await interaction.followup.send(text, view=AttendanceView())
    try:
        db.upsert_roll_call(
            meeting_key=meeting["meetingKey"],
            channel_id=str(sent.channel.id),
            message_id=str(sent.id),
            opened_by=str(interaction.user.id),
        )
    except discord.HTTPException:
        log.exception("Failed to record roll-call message")


@oliver_cmds.command(name="meeting-dashboard", description="Show the next meeting readiness dashboard (admin).")
@admin_only
async def meeting_dashboard_cmd(interaction: discord.Interaction) -> None:
    text = await asyncio.to_thread(meeting_campaign.format_dashboard)
    await interaction.response.send_message(text[:config.MAX_DISCORD_LEN], ephemeral=True)


@oliver_cmds.command(name="proposals", description="Show Oliver's pending action proposals (admin).")
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


@oliver_cmds.command(name="resolve-proposal", description="Accept or dismiss an Oliver proposal (admin).")
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


@oliver_cmds.command(name="memories", description="Search Oliver's durable memories (admin).")
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


@oliver_cmds.command(name="edit-memory", description="Edit one of Oliver's memories (admin).")
@discord.app_commands.describe(memory_id="Memory id from /oliver memories", note="Replacement note")
@admin_only
async def edit_memory_cmd(interaction: discord.Interaction, memory_id: int, note: str) -> None:
    ok = await asyncio.to_thread(db.update_memory, memory_id, note)
    await interaction.response.send_message(
        "Updated memory." if ok else "No active memory with that id.", ephemeral=True)


@oliver_cmds.command(name="forget", description="Delete one of Oliver's memories (admin).")
@discord.app_commands.describe(memory_id="Memory id from /oliver memories")
@admin_only
async def forget_cmd(interaction: discord.Interaction, memory_id: int) -> None:
    ok = await asyncio.to_thread(db.delete_memory, memory_id)
    await interaction.response.send_message(
        "Forgot that memory." if ok else "No active memory with that id.", ephemeral=True)


@oliver_cmds.command(name="tick", description="Run the proactive scheduler now (admin).")
@admin_only
async def oliver_tick(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    n = await run_scheduler()
    await interaction.followup.send(
        f"Posted {n} notification(s)." if n else "Nothing due right now.", ephemeral=True)


# ── Scheduler ────────────────────────────────────────────────────────────────
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
            roll_key = f"roll-call-{meeting['meetingKey']}"
            roll_call = await asyncio.to_thread(db.get_roll_call, meeting["meetingKey"])
            if (
                0 <= days <= meeting_campaign.ROLL_CALL_START_DAYS
                and roll_key not in db.sent_keys()
                and _should_start_scheduled_roll_call(status, roll_call)
            ):
                sent = await main.send(await _roll_call_announcement(status), view=AttendanceView())
                db.upsert_roll_call(
                    meeting_key=meeting["meetingKey"],
                    channel_id=str(sent.channel.id),
                    message_id=str(sent.id),
                    opened_by="scheduler",
                )
                db.mark_sent(roll_key)
                db.add_activity(
                    "roll_call_started",
                    "Roll call started in Discord",
                    f"Meeting: {meeting['meetingKey']}\nChannel: {sent.channel.id}",
                )
                posted += 1
                if email_jmap.enabled():
                    attendance = _attendance_by_member(status)
                    current = sorted(
                        [m for m in corpus_read.members() if m.get("isCurrent")],
                        key=lambda m: m.get("name") or m["slug"],
                    )
                    for member in current:
                        if attendance.get(member["slug"], "pending") != "pending":
                            continue
                        if not db.email_for_member(member["slug"]):
                            continue
                        try:
                            sent_email = await _send_roll_call_email_to_member(member, status)
                            if sent_email:
                                posted += 1
                        except Exception:
                            log.exception("scheduled roll-call email failed for %s", member["slug"])
            alert_key = f"attendance-alert-{meeting['meetingKey']}"
            status = await asyncio.to_thread(meeting_rules.meeting_status)
            if 0 <= days <= 3 and status["recommendation"] != "ready" and alert_key not in db.sent_keys():
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
                db.mark_sent(alert_key)
                db.add_activity(
                    "attendance_alert",
                    "Attendance alert posted",
                    f"Meeting: {meeting['meetingKey']}\nRecommendation: {status['recommendation']}",
                )
                posted += 1

            if email_jmap.enabled():
                campaign = await asyncio.to_thread(meeting_campaign.snapshot)
                candidates = meeting_campaign.reading_checkin_candidates(
                    campaign, today=now.date()
                )
                for candidate in candidates:
                    slug = candidate["memberSlug"]
                    key = f"reading-checkin-{meeting['meetingKey']}-{slug}-{candidate['checkinNumber']}"
                    if key in db.sent_keys():
                        continue
                    member = corpus_read.find_member(slug)
                    if not member or not db.email_for_member(slug):
                        continue
                    try:
                        sent_email = await _send_reading_checkin_email_to_member(
                            member,
                            meeting,
                            note=f"Automated check-in {candidate['checkinNumber']} of {candidate['maxCheckins']}.",
                        )
                    except Exception:
                        log.exception("scheduled reading-checkin email failed for %s", slug)
                        continue
                    if sent_email:
                        db.mark_sent(key)
                        db.add_activity(
                            "reading_checkin_scheduled",
                            "Scheduled reading check-in sent",
                            f"Member: {slug}\nMeeting: {meeting['meetingKey']}\nCheck-in: {candidate['checkinNumber']} of {candidate['maxCheckins']}",
                        )
                        posted += 1

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
    """Kick off the daily loop. Call from on_ready once the gateway is up."""
    if config.MAIN_CHANNEL_ID and not scheduler_loop.is_running():
        scheduler_loop.start()
