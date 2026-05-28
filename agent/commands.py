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
from datetime import datetime, timezone

import discord
from discord.ext import tasks

from agent import (config, context as kb, corpus_read, corpus_write, db,
                   oliver, openlibrary, reviews, scheduler)

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


# ── Modals ───────────────────────────────────────────────────────────────────
class ReviewModal(discord.ui.Modal):
    """The /oliver review form — five inputs, one submit, written to the Git corpus."""

    def __init__(self, slug: str, title: str, existing: dict | None = None) -> None:
        super().__init__(title=f"Review: {title}"[:45])
        self.slug = slug
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
                reviews.write_review, self.slug, interaction.user.display_name,
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
        await interaction.followup.send(
            f"📚 {verb} your review of *{res['book']}* ({score}) — it'll be live on the site shortly.",
            ephemeral=True,
        )


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


@oliver_cmds.command(name="review", description="Log your review of a book the club has read.")
@discord.app_commands.describe(book="The book you're reviewing")
@discord.app_commands.autocomplete(book=book_autocomplete)
async def review_cmd(interaction: discord.Interaction, book: str) -> None:
    member = corpus_read.find_member(interaction.user.display_name)
    if not member:
        await interaction.response.send_message(
            "I can only log reviews from club members — ping Jamie if I've got your name wrong.",
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
    await interaction.response.send_modal(ReviewModal(b["slug"], b["title"], existing))


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
    await interaction.followup.send(
        f"📗 {'Updated' if res['updated'] else 'Added'} **{res['title']}** by {authors} ({cover}). "
        f"Edit `corpus/data/books/{res['slug']}.json` if anything's off, then `/oliver schedule` it.",
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
    await interaction.followup.send(
        f"🗓️ Scheduled **{res['book']}** for {res['date']}, picked by {res['picker']}.", ephemeral=True)


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
    now = datetime.now(timezone.utc)

    # 1. Corpus-derived notifications → main channel.
    main = _client.get_channel(config.MAIN_CHANNEL_ID) if config.MAIN_CHANNEL_ID else None
    if config.MAIN_CHANNEL_ID and main is None:
        log.warning("DISCORD_MAIN_CHANNEL_ID %s not found", config.MAIN_CHANNEL_ID)
    if main is not None:
        due = await asyncio.to_thread(scheduler.due_notifications, now, db.sent_keys())
        for key, msg in due:
            await main.send(msg)
            db.mark_sent(key)
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
            posted += 1
        except discord.HTTPException:
            log.exception("Failed to post reminder %s; will retry next tick", r["id"])

    return posted


@tasks.loop(hours=24)
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
    client.tree.add_command(oliver_cmds)


def start_scheduler() -> None:
    """Kick off the daily loop. Call from on_ready once the gateway is up."""
    if config.MAIN_CHANNEL_ID and not scheduler_loop.is_running():
        scheduler_loop.start()
