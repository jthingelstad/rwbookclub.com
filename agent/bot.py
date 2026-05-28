"""Oliver's Discord plumbing — client lifecycle + message routing + reactions.

Connects to Discord, answers messages in #ask-oliver (and in the main channel
when addressed), routes 👍/👎 reactions to feedback storage. The `/oliver`
slash command group + scheduler loop live in `agent/commands.py`; shared
config (env constants) in `agent/config.py`. Run from the repo root:

    python -m agent.bot

Requires DISCORD_BOT_TOKEN and ANTHROPIC_API_KEY in the root .env, plus the
DISCORD_ASK_OLIVER_CHANNEL_ID / DISCORD_ADMIN_USER_ID identifiers and (for the
scheduler) DISCORD_MAIN_CHANNEL_ID.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from collections import defaultdict

import discord

from agent import commands, config, context as kb, db, gitwrite, oliver

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("oliver")

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
        if config.SERVER_ID:
            guild = discord.Object(id=config.SERVER_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


client = OliverClient()
commands.setup(client)  # attach the /oliver group + stash the client reference
_channel_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def _check_dirty_tree() -> str | None:
    """Return a non-empty status string if the working tree is dirty, else None.

    A dirty tree breaks `gitwrite.sync()` (pull --rebase exits 128), which
    surfaces to the user as a generic "couldn't save that" — the failure we
    actually shipped to the user once. Logging a loud warning at startup
    means future operators see it before a /review attempt does.
    """
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=gitwrite.REPO_ROOT, capture_output=True, text=True,
            check=False, timeout=5,
        )
        return r.stdout.strip() or None
    except (subprocess.SubprocessError, OSError):
        return None


@client.event
async def on_ready() -> None:
    log.info("Oliver connected as %s — %d books in the corpus.", client.user, kb.book_count())
    guilds = list(client.guilds)
    log.info("In %d guild(s): %s", len(guilds), ", ".join(g.name for g in guilds) or "(none)")
    ask = client.get_channel(config.ASK_CHANNEL_ID) if config.ASK_CHANNEL_ID else None
    main = client.get_channel(config.MAIN_CHANNEL_ID) if config.MAIN_CHANNEL_ID else None
    log.info("  ASK_CHANNEL_ID=%s -> %s", config.ASK_CHANNEL_ID, ask)
    log.info("  MAIN_CHANNEL_ID=%s -> %s", config.MAIN_CHANNEL_ID, main)
    dirty = _check_dirty_tree()
    if dirty:
        log.warning(
            "⚠️  Working tree is DIRTY — corpus writes (review, add-book, schedule) "
            "will fail at the gitwrite pull-rebase step. Commit or stash before "
            "letting members exercise write commands. Dirty paths:\n%s",
            dirty[:1000],
        )
    commands.start_scheduler()


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
    if not config.ASK_CHANNEL_ID or cid == config.ASK_CHANNEL_ID:
        question = content.strip()
    # Main channel: answer only when addressed.
    elif config.MAIN_CHANNEL_ID and cid == config.MAIN_CHANNEL_ID:
        is_mention = client.user.mentioned_in(message) and not message.mention_everyone
        has_name = bool(NAME_RE.search(content))
        if not _is_addressed(is_mention, has_name, await _is_reply_to_bot(message)):
            if content.strip():
                db.log_message(str(cid), "user", content.strip(), speaker=message.author.display_name)
            return
        question = _strip_address(content, client.user.id)
    else:
        return

    if not question:
        return

    async with _channel_locks[message.channel.id]:
        async with message.channel.typing():
            try:
                reply = await asyncio.to_thread(
                    oliver.answer, question, str(message.channel.id),
                    message.author.display_name, str(message.author.id), str(message.id),
                )
            except Exception:
                log.exception("Oliver failed to answer")
                reply = "Sorry — I hit a snag answering that. Try me again in a moment."

    # Post the reply. message.reply can fail if the original was deleted, the bot
    # lacks Send Messages / Read Message History, or Discord HTTPs out — try a plain
    # channel.send as a fallback so the user doesn't silently get nothing.
    text = reply[:config.MAX_DISCORD_LEN]
    sent: discord.Message | None = None
    try:
        sent = await message.reply(text, mention_author=False)
    except discord.HTTPException:
        log.exception("message.reply failed in channel %s; trying channel.send", message.channel.id)
        try:
            sent = await message.channel.send(text)
        except discord.HTTPException:
            log.exception("channel.send also failed in %s — user got no reply", message.channel.id)

    # Record what we sent so on_raw_reaction_add can attribute 👍/👎 feedback
    # back to the question that triggered it.
    if sent is not None:
        db.log_response(
            message_id=str(sent.id), channel_id=str(message.channel.id),
            speaker=message.author.display_name, question=question, reply=text,
        )


@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    """Members can 👍 / 👎 any of Oliver's replies; we log and confirm with ✅.

    Uses the raw event (not on_reaction_add) so it works for messages outside
    the cache. We avoid fetching the message until we know it's worth handling
    by checking the responses table first.
    """
    if payload.user_id == client.user.id:
        return  # our own ✅ confirmation
    emoji = str(payload.emoji)
    # Match both bare 👍/👎 and skin-tone variants.
    if emoji.startswith("👍"):
        direction = "up"
    elif emoji.startswith("👎"):
        direction = "down"
    else:
        return
    msg_id = str(payload.message_id)
    if not db.is_oliver_message(msg_id):
        return  # not one of Oliver's replies

    name = payload.member.display_name if payload.member else str(payload.user_id)
    try:
        db.add_feedback(
            message_id=msg_id, channel_id=str(payload.channel_id),
            user_id=str(payload.user_id), user_name=name, reaction=direction,
        )
    except Exception:
        log.exception("Failed to record feedback")
        return

    log.info("feedback: %s %s on message %s", name, direction, msg_id)

    # Confirm receipt. Discord deduplicates ✅ per emoji+user, so re-adding on
    # subsequent feedback is a silent no-op (one checkmark sits on the message).
    try:
        channel = client.get_channel(payload.channel_id) or await client.fetch_channel(payload.channel_id)
        msg = await channel.fetch_message(payload.message_id)
        await msg.add_reaction("✅")
    except discord.HTTPException:
        log.exception("Failed to add ✅ confirmation for message %s", msg_id)


def main() -> None:
    if not config.TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN is not set (add it to the root .env).")
    client.run(config.TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
