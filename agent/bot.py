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

from agent import context as kb, oliver

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("oliver")

TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
ASK_CHANNEL_ID = int(os.environ.get("DISCORD_ASK_OLIVER_CHANNEL_ID") or 0)
ADMIN_USER_ID = int(os.environ.get("DISCORD_ADMIN_USER_ID") or 0)

MAX_DISCORD_LEN = 2000


class OliverClient(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # required to read message text; enable in the Dev Portal too
        super().__init__(intents=intents)
        self.tree = discord.app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        await self.tree.sync()


client = OliverClient()


@client.event
async def on_ready() -> None:
    log.info("Oliver connected as %s — %d books in the corpus.", client.user, kb.book_count())


@client.tree.command(name="ping", description="Check that Oliver is awake.")
async def ping(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("🟢 Oliver is awake.", ephemeral=True)


@client.tree.command(name="corpus", description="Admin: report corpus stats.")
async def corpus(interaction: discord.Interaction) -> None:
    if interaction.user.id != ADMIN_USER_ID:
        await interaction.response.send_message("Sorry, that command is admin-only.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"Corpus holds {kb.book_count()} books. Model: {oliver.MODEL}.", ephemeral=True
    )


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
