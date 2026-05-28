"""Oliver's Discord plumbing.

Connects to Discord, answers messages in the #ask-oliver channel via Claude, and
exposes one `/oliver` command group: open subcommands (ping, review) plus admin
ones gated on DISCORD_ADMIN_USER_ID (stats, add-book, schedule, tick). A daily
scheduler loop posts proactive notifications to DISCORD_MAIN_CHANNEL_ID. Run from
the repo root:

    python -m agent.bot

Requires DISCORD_BOT_TOKEN and ANTHROPIC_API_KEY in the root .env, plus the
DISCORD_ASK_OLIVER_CHANNEL_ID / DISCORD_ADMIN_USER_ID identifiers and (for the
scheduler) DISCORD_MAIN_CHANNEL_ID.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

import discord
from discord.ext import tasks
from dotenv import load_dotenv

from agent import (context as kb, corpus_read, corpus_write, db, oliver,
                   openlibrary, reviews, scheduler)

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("oliver")

TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
ASK_CHANNEL_ID = int(os.environ.get("DISCORD_ASK_OLIVER_CHANNEL_ID") or 0)
ADMIN_USER_ID = int(os.environ.get("DISCORD_ADMIN_USER_ID") or 0)
SERVER_ID = int(os.environ.get("DISCORD_SERVER_ID") or 0)
MAIN_CHANNEL_ID = int(os.environ.get("DISCORD_MAIN_CHANNEL_ID") or 0)

MAX_DISCORD_LEN = 2000

# Oliver answers everything in #ask-oliver, but in the main channel he speaks only
# when addressed — @mentioned, called by name, or replied to.
NAME_RE = re.compile(r"\boliver\b", re.IGNORECASE)


def _is_addressed(is_mention: bool, has_name: bool, is_reply_to_bot: bool) -> bool:
    """Whether a main-channel message is directed at Oliver. Pure (testable)."""
    return bool(is_mention or has_name or is_reply_to_bot)


def _strip_address(content: str, bot_id: int) -> str:
    """Drop @Oliver mentions so the model sees a clean question. Pure (testable)."""
    return re.sub(rf"<@!?{bot_id}>", "", content).strip()


class OliverClient(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # required to read message text; enable in the Dev Portal too
        super().__init__(intents=intents)
        self.tree = discord.app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        # Guild-scoped sync so command changes appear instantly (global sync can
        # take up to an hour to propagate).
        if SERVER_ID:
            guild = discord.Object(id=SERVER_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


client = OliverClient()


@client.event
async def on_ready() -> None:
    log.info("Oliver connected as %s — %d books in the corpus.", client.user, kb.book_count())
    guilds = list(client.guilds)
    log.info("In %d guild(s): %s", len(guilds), ", ".join(g.name for g in guilds) or "(none)")
    ask = client.get_channel(ASK_CHANNEL_ID) if ASK_CHANNEL_ID else None
    main = client.get_channel(MAIN_CHANNEL_ID) if MAIN_CHANNEL_ID else None
    log.info("  ASK_CHANNEL_ID=%s -> %s", ASK_CHANNEL_ID, ask)
    log.info("  MAIN_CHANNEL_ID=%s -> %s", MAIN_CHANNEL_ID, main)
    if MAIN_CHANNEL_ID and not scheduler_loop.is_running():
        scheduler_loop.start()


# All slash commands live under one /oliver group for consistency.
oliver_cmds = discord.app_commands.Group(
    name="oliver", description="Ask Oliver, or help run the R/W Book Club."
)


@oliver_cmds.command(name="ping", description="Check that Oliver is awake.")
async def oliver_ping(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("🟢 Oliver is awake.", ephemeral=True)


@oliver_cmds.command(name="stats", description="Report corpus stats (admin).")
async def oliver_stats(interaction: discord.Interaction) -> None:
    if interaction.user.id != ADMIN_USER_ID:
        await interaction.response.send_message("Sorry, that command is admin-only.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"Corpus holds {kb.book_count()} books. Model: {oliver.MODEL}.", ephemeral=True
    )


async def book_autocomplete(interaction: discord.Interaction, current: str):
    return [
        discord.app_commands.Choice(name=title[:100], value=slug)
        for title, slug in corpus_read.book_choices(current, limit=25)
    ]


class ReviewModal(discord.ui.Modal):
    """The /review form — five inputs, one submit, written to the Git corpus."""

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
        await interaction.response.defer(ephemeral=True)  # the git write may take a few seconds
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
        data, body = corpus_read._parse_frontmatter(rp.read_text())
        existing = {**(data or {}), "review": body}
    await interaction.response.send_modal(ReviewModal(b["slug"], b["title"], existing))


def _is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.id == ADMIN_USER_ID


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


@oliver_cmds.command(name="add-book", description="Add a book to the corpus (admin) — fetches metadata from Open Library.")
@discord.app_commands.describe(title="Book title", isbn="ISBN (optional, more precise)")
async def oliver_add_book(interaction: discord.Interaction, title: str, isbn: str | None = None) -> None:
    if not _is_admin(interaction):
        await interaction.response.send_message("That's an admin command.", ephemeral=True)
        return
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
async def oliver_schedule(interaction: discord.Interaction, book: str, date: str, picker: str) -> None:
    if not _is_admin(interaction):
        await interaction.response.send_message("That's an admin command.", ephemeral=True)
        return
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


async def run_scheduler() -> int:
    """Post any due proactive notifications to the main channel; returns the count posted."""
    if not MAIN_CHANNEL_ID:
        return 0
    channel = client.get_channel(MAIN_CHANNEL_ID)
    if channel is None:
        log.warning("DISCORD_MAIN_CHANNEL_ID %s not found", MAIN_CHANNEL_ID)
        return 0
    from datetime import datetime, timezone
    due = await asyncio.to_thread(scheduler.due_notifications, datetime.now(timezone.utc), db.sent_keys())
    posted = 0
    for key, msg in due:
        await channel.send(msg)
        db.mark_sent(key)
        posted += 1
    return posted


@tasks.loop(hours=24)
async def scheduler_loop() -> None:
    try:
        n = await run_scheduler()
        if n:
            log.info("scheduler posted %d notification(s)", n)
    except Exception:
        log.exception("scheduler loop error")


@oliver_cmds.command(name="tick", description="Run the proactive scheduler now (admin).")
async def oliver_tick(interaction: discord.Interaction) -> None:
    if not _is_admin(interaction):
        await interaction.response.send_message("That's an admin command.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    n = await run_scheduler()
    await interaction.followup.send(
        f"Posted {n} notification(s)." if n else "Nothing due right now.", ephemeral=True)


client.tree.add_command(oliver_cmds)


async def _is_reply_to_bot(message: discord.Message) -> bool:
    """True if this message is a reply to one of Oliver's own messages."""
    ref = message.reference
    if not ref:
        return False
    if isinstance(ref.resolved, discord.Message):
        return ref.resolved.author.id == client.user.id
    if ref.message_id:  # not cached — fetch it
        try:
            referenced = await message.channel.fetch_message(ref.message_id)
        except (discord.NotFound, discord.HTTPException):
            return False
        return referenced.author.id == client.user.id
    return False


@client.event
async def on_message(message: discord.Message) -> None:
    # Ignore our own messages and other bots.
    if message.author.bot or message.author == client.user:
        return

    cid = message.channel.id
    content = message.content or ""

    # #ask-oliver: answer everything. (No ask-channel configured → answer everywhere, dev.)
    if not ASK_CHANNEL_ID or cid == ASK_CHANNEL_ID:
        question = content.strip()
    # Main channel: answer only when addressed.
    elif MAIN_CHANNEL_ID and cid == MAIN_CHANNEL_ID:
        is_mention = client.user.mentioned_in(message) and not message.mention_everyone
        has_name = bool(NAME_RE.search(content))
        if not _is_addressed(is_mention, has_name, await _is_reply_to_bot(message)):
            return
        question = _strip_address(content, client.user.id)
    else:
        return

    if not question:
        return

    async with message.channel.typing():
        try:
            reply = await asyncio.to_thread(
                oliver.answer, question, str(message.channel.id), message.author.display_name
            )
        except Exception:
            log.exception("Oliver failed to answer")
            reply = "Sorry — I hit a snag answering that. Try me again in a moment."
    await message.reply(reply[:MAX_DISCORD_LEN], mention_author=False)


def main() -> None:
    if not TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN is not set (add it to the root .env).")
    client.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
