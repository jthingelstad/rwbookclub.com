"""The `/oliver` slash-command groups and persistent interaction views.

Lives separately from bot.py so the Discord plumbing (client, lifecycle, message
routing, reactions) stays focused. `setup(client)` wires the command group into
the client's tree; proactive scheduling lives in :mod:`agent.proactive`.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging

import discord

from agent import (
    clubdb,
    config,
    corpus_read,
    db,
    identities,
    jobs,
    oliver,
    proactive,
    webapp,
)
from agent import context as kb
from agent.club import (
    meeting_campaign,
    meeting_emails,
    meeting_rules,
    outreach,
    release_notes,
)
from agent.mail import email_jmap, mail_archive, outbound

log = logging.getLogger("oliver.commands")


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
            await interaction.response.send_message("That's an admin command.", ephemeral=True)
            return
        return await func(interaction, *args, **kwargs)

    return wrapper


# ── The /oliver group ────────────────────────────────────────────────────────
oliver_cmds = discord.app_commands.Group(
    name="oliver", description="Ask Oliver, or help run the R/W Book Club."
)

# Domain subcommand groups, so `/oliver` reads as a handful of purposes rather than a flat list.
# discord.py nests these under oliver_cmds automatically (parent=), within Discord's 2-level limit:
# `/oliver <group> <subcommand> [options]`.
# 2026-07 command review: the `contact`, `memory`, and `library` groups were retired — member
# self-service and admin data edits live in the web app (/oliver my-club) now; memory grooming
# has its own admin webapp page; link-member (needs Discord's user picker) moved under `admin`.
reading_cmds = discord.app_commands.Group(
    name="reading", description="Your reading progress for the next book.", parent=oliver_cmds
)
meeting_cmds = discord.app_commands.Group(
    name="meeting",
    description="Run the next meeting — roll call, reading check-ins, readiness.",
    parent=oliver_cmds,
)
timeline_cmds = discord.app_commands.Group(
    name="timeline",
    description="The club's event timeline — view it or record an event.",
    parent=oliver_cmds,
)
admin_cmds = discord.app_commands.Group(
    name="admin",
    description="Operate Oliver — status, feedback, proposals, scheduler (admin).",
    parent=oliver_cmds,
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
    slug = identities.member_slug_for_user(str(user_id))
    return corpus_read.find_member(slug) if slug else None


def _reading_status_text() -> str:
    meeting = meeting_rules.next_meeting()
    book = meeting.get("book") or {}
    title = book.get("title") or "the current book"
    meeting_id = meeting["meetingId"]
    rows = _reading_status_by_member(meeting_id)
    current = sorted(
        corpus_read.human_current_members(),
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


def _admin_check_message(interaction: discord.Interaction) -> str | None:
    return None if _is_admin(interaction) else "That's an admin command."


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
        oliver.compose,
        "roll-call announcement for the club channel",
        facts,
        fallback=_roll_call_message(status),
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
        oliver.compose,
        "roll-call reminder for the club channel",
        facts,
        fallback="🔔 Roll call reminder.\n\n" + meeting_rules.format_status(status),
    )


async def _email_roll_call(interaction: discord.Interaction) -> None:
    if not email_jmap.enabled():
        await interaction.response.send_message("Email is not configured.", ephemeral=True)
        return
    status = meeting_rules.meeting_status()
    meeting = status["meeting"]
    current = sorted(
        corpus_read.human_current_members(),
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
        email = identities.email_for_member(member["slug"])
        if not email:
            missing.append(member["name"])
            continue
        try:
            await outreach.send_roll_call_email(
                member,
                status,
                idempotency_key=f"email:manual-roll-call:{interaction.id}:{member['slug']}",
            )
            sent.append(member["name"])
        except Exception:
            log.exception("roll-call email failed for %s", member["slug"])
            missing.append(f"{member['name']} (send failed)")
    if (
        sent
        and meeting["meetingId"] is not None
        and not db.has_open_roll_call(meeting["meetingId"])
    ):
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

    @discord.ui.button(
        label="I'll be there", style=discord.ButtonStyle.success, custom_id="oliver:attendance:yes"
    )
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await record_attendance_response(interaction, "yes")

    @discord.ui.button(
        label="I can't make it", style=discord.ButtonStyle.danger, custom_id="oliver:attendance:no"
    )
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await record_attendance_response(interaction, "no")

    @discord.ui.button(
        label="Unsure", style=discord.ButtonStyle.secondary, custom_id="oliver:attendance:unsure"
    )
    async def unsure(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await record_attendance_response(interaction, "unsure")


async def record_attendance_response(interaction: discord.Interaction, status: str) -> None:
    member = _linked_member_for_user(interaction.user.id)
    if not member:
        await interaction.response.send_message(
            "I don't have your Discord account linked to a current club member yet.", ephemeral=True
        )
        return
    meeting = meeting_rules.next_meeting()
    meeting_id = meeting["meetingId"]
    member_id = clubdb.lookup_member_id(member["slug"])
    if meeting_id is None or member_id is None:
        await interaction.response.send_message(
            "There's no scheduled meeting to record against yet.", ephemeral=True
        )
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
        ephemeral=True,
    )


PRIVATE_FEEDBACK_CONFIRMATION = (
    "Saved privately in Oliver's memory for future pick and recommendation context. This does "
    "not change your public review and will not appear on the club website."
)


def _save_private_book_feedback(
    *, user_id: int | str, book_value: str, note: str, source_message_id: str | None = None
) -> dict:
    """Private member-memory write only; deliberately has no review/corpus/publish path."""
    member = _linked_member_for_user(int(user_id))
    if not member:
        raise ValueError("I can only save private feedback for a linked club member.")
    book = corpus_read.find_book(book_value)
    if not book:
        raise ValueError("I couldn't find that book in the club record.")
    text = note.strip()
    if not text:
        raise ValueError("Private feedback needs a note.")
    book_slug = book["slug"]
    book_title = book["title"]
    memory_id = db.add_memory(
        f"Private book feedback on {book_title} ({book_slug}): {text}",
        scope="member",
        subject=member["slug"],
        source="private_book_feedback",
        source_user_id=str(user_id),
        source_message_id=source_message_id,
    )
    return {
        "id": memory_id,
        "member_slug": member["slug"],
        "book_slug": book_slug,
        "book_title": book_title,
    }


class PrivateBookFeedbackModal(discord.ui.Modal):
    note = discord.ui.Label(
        text="What should Oliver remember?",
        component=discord.ui.TextInput(
            placeholder="Fit, discussion, DNF reason, or what this means for future picks…",
            style=discord.TextStyle.paragraph,
            min_length=1,
            max_length=2000,
            required=True,
        ),
    )

    def __init__(self, *, book: dict) -> None:
        title = f"Private note: {book['title']}"[:45]
        super().__init__(title=title)
        self.book_slug = book["slug"]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            _save_private_book_feedback(
                user_id=interaction.user.id,
                book_value=self.book_slug,
                note=str(self.note.component.value),
                source_message_id=str(interaction.id),
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(PRIVATE_FEEDBACK_CONFIRMATION, ephemeral=True)


# ── Subcommands ──────────────────────────────────────────────────────────────
@oliver_cmds.command(name="ping", description="Check that Oliver is awake.")
async def oliver_ping(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("🟢 Oliver is awake.", ephemeral=True)


@oliver_cmds.command(
    name="my-club",
    description="Get your private link to manage your club stuff — ratings, reviews, lists, profile.",
)
async def oliver_webapp(interaction: discord.Interaction) -> None:
    member = _linked_member_for_user(interaction.user.id)
    if not member:
        await interaction.response.send_message(_LINK_FIRST, ephemeral=True)
        return
    member_id = clubdb.lookup_member_id(member["slug"])
    if member_id is None:
        await interaction.response.send_message(
            "I couldn't resolve your member record — ask an admin to check your link.",
            ephemeral=True,
        )
        return
    await webapp.ensure_running()  # spin the server up on demand (it idles off when unused)
    token = await asyncio.to_thread(webapp.mint_token, member_id, is_admin=_is_admin(interaction))
    url = f"{config.WEBAPP_BASE_URL}/webapp?t={token}"
    # Angle brackets suppress Discord's link unfurl, so its preview bot won't pre-fetch (and burn)
    # the single-use token before you tap it. The server also ignores preview bots as a backstop.
    await interaction.response.send_message(
        f"🔧 Your private link (good for ~15 minutes, just for you):\n<{url}>\n"
        "_Blank page? The editor sleeps after ~15 min idle (or a restart) — just run "
        "`/oliver my-club` again to wake it._",
        ephemeral=True,
    )


@oliver_cmds.command(
    name="private-feedback",
    description="Privately tell Oliver what to remember about a book (not a public review).",
)
@discord.app_commands.describe(book="Book from the club record")
@discord.app_commands.autocomplete(book=book_autocomplete)
async def private_feedback_cmd(interaction: discord.Interaction, book: str) -> None:
    if not _linked_member_for_user(interaction.user.id):
        await interaction.response.send_message(
            "I can only save private book feedback for linked club members — ask an admin to "
            "link your Discord account first.",
            ephemeral=True,
        )
        return
    resolved = corpus_read.find_book(book)
    if not resolved:
        await interaction.response.send_message(
            "I couldn't find that book in the club record, so I didn't save anything.",
            ephemeral=True,
        )
        return
    await interaction.response.send_modal(PrivateBookFeedbackModal(book=resolved))


@admin_cmds.command(
    name="status", description="Oliver at a glance — release, models, data, memory (admin)."
)
@admin_only
async def oliver_status(interaction: discord.Interaction) -> None:
    release = db.current_release()
    release_line = (
        f"**Release:** {release['name']} (`{release['commit']}`, "
        f"{(release.get('occurred_at') or '')[:10]})"
        if release
        else "**Release:** unnamed (no christened release yet)"
    )
    memories = await asyncio.to_thread(db.count_memories)
    reflected = db.get_job_state("reflection") or {}  # 'ran_at' stamped by each reflection pass
    proposals = await asyncio.to_thread(db.list_proposals, 10)
    job_rows = await asyncio.to_thread(jobs.status)
    lines = [
        release_line,
        f"**Models:** chat {oliver.MODEL} · generate {oliver.OPUS_MODEL}",
        f"**Club record:** {kb.book_count()} books · {len(corpus_read.meetings())} meetings",
        f"**Memory:** {memories} active memories · last reflection "
        f"{(reflected.get('ran_at') or 'never')[:10]}",
        f"**Proposals pending:** {len(proposals)}",
        "**Scheduled jobs:**",
        *jobs.format_status(job_rows).splitlines(),
    ]
    await interaction.response.send_message(
        "\n".join(lines)[: config.MAX_DISCORD_LEN], ephemeral=True
    )


@admin_cmds.command(
    name="release-notes", description="Draft & send release notes from recent changes (admin)."
)
@discord.app_commands.describe(
    days="Look back this many days (1-90). Ignored if 'since' is set.",
    since="Or scope to changes since this git commit (hash or ref).",
    to="Where to send — yourself (default) or the club mailing list",
)
# Default scope (no days, no since): everything since the last release notes — or the last
# 7 days if none has ever been sent. A list send records HEAD as the next baseline.
@discord.app_commands.choices(
    to=[
        discord.app_commands.Choice(name="me", value="me"),
        discord.app_commands.Choice(name="list", value="list"),
    ]
)
@admin_only
async def release_notes_cmd(
    interaction: discord.Interaction,
    days: int | None = None,
    since: str | None = None,
    to: discord.app_commands.Choice[str] | None = None,
) -> None:
    if since:
        resolved = release_notes.resolve_commit(since)
        if not resolved:
            await interaction.response.send_message(
                f"I couldn't find a commit matching `{since}`.", ephemeral=True
            )
            return
        days_arg, since_arg, scope = None, resolved, f"since `{resolved}`"
    elif (
        days is None
        and (baseline := db.last_release_notes_commit())
        and release_notes.resolve_commit(baseline)
    ):
        # No explicit scope → pick up where the last release notes left off, so every change
        # since then is covered exactly once.
        days_arg, since_arg, scope = None, baseline, f"since the last release notes (`{baseline}`)"
    else:
        d = days if days is not None else 7
        if not 1 <= d <= 90:
            await interaction.response.send_message(
                "Pick a window between 1 and 90 days.", ephemeral=True
            )
            return
        days_arg, since_arg, scope = d, None, f"the last {d} days"
    if not email_jmap.enabled():
        await interaction.response.send_message(
            "Email isn't configured (no FASTMAIL_JMAP_TOKEN), so I can't send release notes.",
            ephemeral=True,
        )
        return

    target = to.value if to else "me"
    await interaction.response.defer(ephemeral=True)
    try:
        email = await asyncio.to_thread(
            release_notes.release_notes_email, days=days_arg, since_commit=since_arg
        )
    except Exception:
        log.exception("release-notes generation failed")
        db.add_activity(
            "warning",
            "Release notes failed",
            f"Scope: {scope}\nGenerating the release-notes email raised an exception.",
        )
        await interaction.followup.send(
            "Something went wrong drafting the release notes.", ephemeral=True
        )
        return
    if email is None:
        await interaction.followup.send(
            f"No changes {scope} — nothing to announce.", ephemeral=True
        )
        return

    try:
        name = email.get("release_name") or None
        christened = f"\nChristened: *{name}*" if name else ""
        if target == "list":
            await proactive.send_club_email(
                email["subject"],
                email["body"],
                idempotency_key=f"club-email:release-notes:{interaction.id}",
            )
            db.add_activity(
                "release_notes_sent",
                "Release notes sent",
                f"Scope: {scope}\nTo: club mailing list\nSubject: {email['subject']}"
                + (f"\nRelease name: {name}" if name else ""),
            )
            # Mark this release in the club timeline and store HEAD as the baseline the next
            # release-notes scopes from (so it auto-covers everything shipped since). A named
            # list send is what christens the release — the name becomes current_release().
            head = release_notes.head_commit()
            gh_url = None
            if head:
                db.record_release_notes_sent(
                    head,
                    scope=scope,
                    subject=email["subject"],
                    window=email.get("window"),
                    release_name=name,
                )
                # The permanent code reference: tag + GitHub release named after the christening.
                # Best-effort (the email already went); failures land in #oliver-log.
                gh_url = await asyncio.to_thread(
                    release_notes.create_github_release,
                    name=name or "",
                    commit=head,
                    body=email["body"],
                )
            gh_line = f"\nGitHub release: {gh_url}" if gh_url else ""
            await interaction.followup.send(
                f"📣 Sent release notes ({scope}) to the club list and mirrored to the main "
                f"channel.\nSubject: *{email['subject']}*{christened}{gh_line}",
                ephemeral=True,
            )
        else:
            slug = identities.member_slug_for_user(str(interaction.user.id))
            rec = identities.email_for_member(slug) if slug else None
            if not rec:
                await interaction.followup.send(
                    "You don't have a linked email address, so I can only send this to the list "
                    "(`to:list`) — or link an email first.",
                    ephemeral=True,
                )
                return
            sent = await asyncio.to_thread(
                outbound.send,
                to=[rec["email"]],
                subject=email["subject"],
                body=email["body"],
                idempotency_key=f"email:release-notes-draft:{interaction.id}",
                policy="linked_member",
            )
            db.add_activity(
                "release_notes_sent",
                "Release notes sent",
                f"Scope: {scope}\nTo: {rec['email']} (admin)\nSubject: {email['subject']}\n"
                f"Email ID: {sent.get('emailId')}",
            )
            await interaction.followup.send(
                f"📝 Emailed the release-notes draft ({scope}) to `{rec['email']}` "
                f"(`{sent.get('emailId')}`).\nSubject: *{email['subject']}*"
                + (
                    f"\nDraft release name: *{name}* (christened only on a list send)"
                    if name
                    else ""
                )
                + "\nReview it, then re-run with `to:list` to send it to the club.",
                ephemeral=True,
            )
    except Exception:
        log.exception("release-notes send failed")
        db.add_activity(
            "warning",
            "Release notes send failed",
            f"Days: {days}\nTo: {target}\nThe draft was generated but sending failed.",
        )
        await interaction.followup.send(
            "I drafted the notes but couldn't send the email.", ephemeral=True
        )


@admin_cmds.command(
    name="postscript",
    description="Draft the after-meeting 'Postscript' digest and email it to you (admin test).",
)
@admin_only
async def postscript_cmd(interaction: discord.Interaction) -> None:
    if not email_jmap.enabled():
        await interaction.response.send_message(
            "Email isn't configured (no FASTMAIL_JMAP_TOKEN), so I can't draft Postscript.",
            ephemeral=True,
        )
        return
    slug = identities.member_slug_for_user(str(interaction.user.id))
    rec = identities.email_for_member(slug) if slug else None
    if not rec:
        await interaction.response.send_message(
            "You don't have a linked email address — link one in the web app first "
            "(`/oliver my-club` → Profile).",
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True)
    try:
        email = await asyncio.to_thread(meeting_emails.postscript_email)
    except Exception:
        log.exception("postscript generation failed")
        db.add_activity(
            "warning",
            "Postscript draft failed",
            "Generating the Postscript digest raised an exception.",
        )
        await interaction.followup.send("Something went wrong drafting Postscript.", ephemeral=True)
        return
    try:
        # Self-only test send — to the admin's own linked email, never the club list. Bypasses the
        # flag/window/dedup and records no group event, so it can be re-run freely while iterating.
        sent = await asyncio.to_thread(
            outbound.send,
            to=[rec["email"]],
            subject=email["subject"],
            body=email["body"],
            idempotency_key=f"email:postscript-draft:{interaction.id}",
            policy="linked_member",
        )
    except Exception:
        log.exception("postscript send failed")
        await interaction.followup.send(
            "I drafted Postscript but couldn't send the email.", ephemeral=True
        )
        return
    db.add_activity(
        "club_email_sent",
        "Postscript test draft sent",
        f"To: {rec['email']} (admin test)\nItems offered: {len(email['offered'])}\n"
        f"Email ID: {sent.get('emailId')}",
    )
    await interaction.followup.send(
        f"📝 Emailed a Postscript draft to `{rec['email']}` (`{sent.get('emailId')}`) — test only, "
        "not the club. Check the items are real, then set `CLUB_POSTSCRIPT_ENABLED=1` for the real "
        "~1-week-after-meeting send.",
        ephemeral=True,
    )


@admin_cmds.command(name="link-member", description="Link a Discord user to a club member (admin).")
@discord.app_commands.describe(member="Club member", user="Discord user")
@discord.app_commands.autocomplete(member=member_autocomplete)
@admin_only
async def link_member_cmd(
    interaction: discord.Interaction, member: str, user: discord.User
) -> None:
    m = corpus_read.find_member(member)
    if not m:
        await interaction.response.send_message("I couldn't find that member.", ephemeral=True)
        return
    identities.link_member_identity(str(user.id), m["slug"], linked_by=str(interaction.user.id))
    await interaction.response.send_message(
        f"Linked {user.mention} to {m['name']} (`{m['slug']}`).", ephemeral=True
    )


# Everyone manages contact handles, lists, and ratings/reviews in the web app now
# (/oliver my-club — email/SMS linking re-attributes archived mail there too). Only link-member
# stays a slash command: it needs Discord's user picker. _LINK_FIRST is used by /oliver my-club
# for unlinked callers.
_LINK_FIRST = (
    "I can only do that for linked club members — ask an admin to run "
    "`/oliver admin link-member` to connect your Discord account first."
)


@admin_cmds.command(
    name="reattribute-mail", description="Re-resolve archived mail senders to members (admin)."
)
@admin_only
async def reattribute_mail_cmd(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    changed = await asyncio.to_thread(mail_archive.reattribute_archive)
    await interaction.followup.send(
        f"Re-attributed {changed} archived message(s) to their current member links."
        if changed
        else "Archive attribution already up to date — nothing changed.",
        ephemeral=True,
    )


@oliver_cmds.command(name="whoami", description="Check which club member Oliver has linked you to.")
async def whoami_cmd(interaction: discord.Interaction) -> None:
    member = _linked_member_for_user(interaction.user.id)
    if member:
        await interaction.response.send_message(
            f"I have you linked as {member['name']}.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        "I don't have your Discord account linked to a club member yet.", ephemeral=True
    )


@reading_cmds.command(
    name="status", description="Show everyone's progress on the next book, or update yours."
)
@discord.app_commands.describe(
    status="Optional status: not_started, started, on_track, behind, finished, paused",
    progress="Optional short note, e.g. 'chapter 6' or 'halfway'",
    page="Optional page number",
    percent="Optional percent complete",
)
async def reading_status_cmd(
    interaction: discord.Interaction,
    status: str | None = None,
    progress: str | None = None,
    page: int | None = None,
    percent: int | None = None,
) -> None:
    if not any(v is not None for v in (status, progress, page, percent)):
        await interaction.response.send_message(_reading_status_text(), ephemeral=True)
        return
    member = _linked_member_for_user(interaction.user.id)
    if not member:
        await interaction.response.send_message(
            "I can only update reading status from linked club members.", ephemeral=True
        )
        return
    normalized = (status or "started").strip().lower().replace("-", "_").replace(" ", "_")
    try:
        meeting = meeting_rules.next_meeting()
        meeting_id = meeting["meetingId"]
        member_id = clubdb.lookup_member_id(member["slug"])
        if meeting_id is None or member_id is None:
            await interaction.response.send_message(
                "There's no scheduled meeting to record reading status against yet.", ephemeral=True
            )
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


@meeting_cmds.command(
    name="check-in", description="Email a reading-status check-in to a member (admin)."
)
@discord.app_commands.describe(member="Club member", note="Optional extra sentence")
@discord.app_commands.autocomplete(member=member_autocomplete)
@admin_only
async def reading_checkin_cmd(
    interaction: discord.Interaction, member: str, note: str | None = None
) -> None:
    m = corpus_read.find_member(member)
    if not m:
        await interaction.response.send_message("I couldn't find that member.", ephemeral=True)
        return
    email = identities.email_for_member(m["slug"])
    if not email:
        await interaction.response.send_message(
            f"{m['name']} does not have a linked email address.", ephemeral=True
        )
        return
    if not email_jmap.enabled():
        await interaction.response.send_message("Email is not configured.", ephemeral=True)
        return
    meeting = meeting_rules.next_meeting()
    book = meeting.get("book") or {}
    title = book.get("title") or "the current book"
    meeting_id = meeting["meetingId"]
    member_id = clubdb.lookup_member_id(m["slug"])
    existing = (
        db.meeting_member_status(meeting_id, member_id)
        if (meeting_id is not None and member_id is not None)
        else None
    )
    if existing and existing["reading"] == "finished":
        db.add_activity(
            "reading_checkin_skipped",
            "Reading check-in skipped",
            f"Member: {m['slug']}\nReason: already finished\nBook: {title}",
        )
        await interaction.response.send_message(
            f"{m['name']} is already marked finished for {title}; not sending a check-in.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True)
    try:
        sent = await outreach.send_reading_checkin_email(
            m,
            meeting,
            note=note,
            idempotency_key=f"email:manual-reading-checkin:{interaction.id}:{m['slug']}",
        )
    except Exception:
        log.exception("reading-checkin email failed")
        await interaction.followup.send("I couldn't send that email.", ephemeral=True)
        return
    await interaction.followup.send(
        f"Sent reading check-in to {m['name']} at `{email['email']}` (`{sent.get('emailId')}`).",
        ephemeral=True,
    )


@timeline_cmds.command(name="log", description="Record a club timeline event (admin).")
@discord.app_commands.describe(
    category="Event category",
    kind="Event kind, e.g. dinner, book_picked, member_away, member_milestone, meeting_held",
    date="When it happened (YYYY-MM-DD)",
    summary="One factual sentence",
    member="Optional member the event is about (omit for club-wide)",
)
@discord.app_commands.choices(
    category=[
        discord.app_commands.Choice(name=c, value=c)
        for c in ("meeting", "selection", "social", "member_life", "club", "reading")
    ]
)
@discord.app_commands.autocomplete(member=member_autocomplete)
@admin_only
async def oliver_log_event(
    interaction: discord.Interaction,
    category: discord.app_commands.Choice[str],
    kind: str,
    date: str,
    summary: str,
    member: str | None = None,
) -> None:
    cat = category.value
    allowed = db.CHRONICLE_KINDS.get(cat) or ()
    if kind not in allowed:
        await interaction.response.send_message(
            f"`{kind}` isn't a valid kind for **{cat}**. Allowed: {', '.join(allowed)}.",
            ephemeral=True,
        )
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
        db.record_event,
        actor="admin",
        surface="discord",
        kind=kind,
        category=cat,
        member_id=member_id,
        detail={"summary": summary, "members": [member_slug] if member_slug else []},
        occurred_at=(date or "")[:10] or None,
    )
    who = f" for {member_slug}" if member_slug else " (club-wide)"
    await interaction.response.send_message(
        f"🗓️ Logged **{cat}/{kind}** on {date}{who} (#{eid}).", ephemeral=True
    )


@timeline_cmds.command(name="show", description="Show recent club timeline events.")
@discord.app_commands.describe(member="Optional member to scope to", category="Optional category")
@discord.app_commands.autocomplete(member=member_autocomplete)
@discord.app_commands.choices(
    category=[
        discord.app_commands.Choice(name=c, value=c)
        for c in ("meeting", "selection", "social", "member_life", "club", "reading", "meeting_ops")
    ]
)
async def oliver_timeline(
    interaction: discord.Interaction,
    member: str | None = None,
    category: discord.app_commands.Choice[str] | None = None,
) -> None:
    member_id = None
    if member:
        m = corpus_read.find_member(member)
        if not m:
            await interaction.response.send_message("I couldn't find that member.", ephemeral=True)
            return
        member_id = clubdb.lookup_member_id(m["slug"])
    rows = await asyncio.to_thread(
        db.timeline, category=(category.value if category else None), member_id=member_id, limit=20
    )
    if not rows:
        await interaction.response.send_message("No timeline events recorded yet.", ephemeral=True)
        return
    lines = ["**Club timeline** (most recent):"]
    for r in rows:
        detail = r.get("detail") or ""
        try:
            summary = json.loads(detail).get("summary") if detail.startswith("{") else detail
        except json.JSONDecodeError, AttributeError:
            summary = detail
        who = f" [{r['member_slug']}]" if r.get("member_slug") else ""
        lines.append(f"• {(r.get('occurred_at') or '')[:10]} ({r['kind']}){who}: {summary}")
    await interaction.response.send_message(
        "\n".join(lines)[: config.MAX_DISCORD_LEN], ephemeral=True
    )


@admin_cmds.command(
    name="feedback", description="Recent 👍/👎 feedback on Oliver's replies (admin)."
)
@admin_only
async def oliver_feedback(interaction: discord.Interaction) -> None:
    stats = await asyncio.to_thread(db.feedback_stats)
    if not stats["total"]:
        await interaction.response.send_message(
            "No feedback yet — members can 👍/👎 any of my replies and I'll log it.", ephemeral=True
        )
        return
    lines = [
        f"📊 **Feedback to date:** {stats['up']} 👍 · {stats['down']} 👎  ({stats['total']} total)"
    ]
    if stats["recent_down"]:
        lines.append("\n**Recent 👎:**")
        for r in stats["recent_down"]:
            q = (r.get("question") or "(no question recorded)")[:100]
            lines.append(f'• {r["user_name"]}: "{q}"')
    if stats["recent_up"]:
        lines.append("\n**Recent 👍:**")
        for r in stats["recent_up"]:
            q = (r.get("question") or "(no question recorded)")[:100]
            lines.append(f'• {r["user_name"]}: "{q}"')
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@meeting_cmds.command(
    name="roll-call", description="Start, check, remind, or close meeting roll call."
)
@discord.app_commands.describe(action="What to do with the current meeting roll call")
@discord.app_commands.choices(
    action=[
        discord.app_commands.Choice(name="status", value="status"),
        discord.app_commands.Choice(name="start", value="start"),
        discord.app_commands.Choice(name="remind", value="remind"),
        discord.app_commands.Choice(name="email", value="email"),
        discord.app_commands.Choice(name="close", value="close"),
    ]
)
async def roll_call_cmd(
    interaction: discord.Interaction, action: discord.app_commands.Choice[str]
) -> None:
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
            ephemeral=True,
        )
        return

    if act == "close":
        if meeting["meetingId"] is not None and db.has_open_roll_call(meeting["meetingId"]):
            db.record_group_event(meeting["meetingId"], "roll_call_closed", actor="admin")
        await interaction.response.send_message(
            meeting_rules.format_status(meeting_rules.meeting_status(meeting["meetingId"])),
            ephemeral=True,
        )
        return

    if act == "email":
        await _email_roll_call(interaction)
        return

    # Both start and remind post the roll call with attendance buttons, in Oliver's
    # voice — defer past the 3s ack window before composing.
    status = meeting_rules.meeting_status(meeting["meetingId"])
    await interaction.response.defer()
    text = await (
        _roll_call_announcement(status) if act == "start" else _roll_call_reminder(status)
    )
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


@meeting_cmds.command(
    name="dashboard", description="Show the next meeting readiness dashboard (admin)."
)
@admin_only
async def meeting_dashboard_cmd(interaction: discord.Interaction) -> None:
    text = await asyncio.to_thread(meeting_campaign.format_dashboard)
    await interaction.response.send_message(text[: config.MAX_DISCORD_LEN], ephemeral=True)


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
    await interaction.response.send_message(
        "\n".join(lines)[: config.MAX_DISCORD_LEN], ephemeral=True
    )


@admin_cmds.command(name="resolve", description="Accept or dismiss an Oliver proposal (admin).")
@discord.app_commands.describe(
    proposal_id="Proposal id from /oliver proposals", decision="Accept or dismiss"
)
@discord.app_commands.choices(
    decision=[
        discord.app_commands.Choice(name="accept", value="accepted"),
        discord.app_commands.Choice(name="dismiss", value="dismissed"),
    ]
)
@admin_only
async def resolve_proposal_cmd(
    interaction: discord.Interaction, proposal_id: int, decision: discord.app_commands.Choice[str]
) -> None:
    ok = await asyncio.to_thread(
        db.resolve_proposal, proposal_id, decision.value, resolved_by=str(interaction.user.id)
    )
    await interaction.response.send_message(
        f"Proposal {decision.name}ed." if ok else "No pending proposal with that id.",
        ephemeral=True,
    )


@admin_cmds.command(name="tick", description="Run the proactive scheduler now (admin).")
@admin_only
async def oliver_tick(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    n = await proactive.run()
    await interaction.followup.send(
        f"Posted {n} notification(s)." if n else "Nothing due right now.", ephemeral=True
    )


# ── Wiring ───────────────────────────────────────────────────────────────────
def setup(client: discord.Client) -> None:
    """Attach the command group and persistent views to the client's tree."""
    client.add_view(AttendanceView())
    client.tree.add_command(oliver_cmds)
