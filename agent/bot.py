"""Oliver's Discord plumbing.

Connects to Discord, answers messages in the #ask-oliver channel via Claude,
exposes a /ping health check, and gates an admin-only /corpus command on
DISCORD_ADMIN_USER_ID. Run from the repo root:

    python -m agent.bot

Requires DISCORD_BOT_TOKEN and ANTHROPIC_API_KEY in the root .env, plus the
DISCORD_ASK_OLIVER_CHANNEL_ID / DISCORD_ADMIN_USER_ID identifiers.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import discord
from dotenv import load_dotenv

from agent import context as kb, corpus_read, oliver, reviews

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("oliver")

TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
ASK_CHANNEL_ID = int(os.environ.get("DISCORD_ASK_OLIVER_CHANNEL_ID") or 0)
ADMIN_USER_ID = int(os.environ.get("DISCORD_ADMIN_USER_ID") or 0)
SERVER_ID = int(os.environ.get("DISCORD_SERVER_ID") or 0)

MAX_DISCORD_LEN = 2000


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


client.tree.add_command(oliver_cmds)


@client.event
async def on_message(message: discord.Message) -> None:
    # Ignore our own messages and other bots.
    if message.author.bot or message.author == client.user:
        return
    # Only answer in the dedicated ask-oliver channel.
    if ASK_CHANNEL_ID and message.channel.id != ASK_CHANNEL_ID:
        return
    question = message.content.strip()
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
