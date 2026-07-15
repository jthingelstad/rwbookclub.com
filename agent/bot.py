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
import os
import pathlib
import re
import subprocess
import sys
from collections import defaultdict

import discord
import requests
from discord.ext import tasks

from agent import commands, config, context as kb, db, oliver, outbox, publish, security
from agent.mail import email_jmap
from agent.mail import inbound as inbound_email

# Split the streams so launchd's logs are useful: activity (DEBUG/INFO) → stdout (oliver.log),
# problems (WARNING+) → stderr (oliver.err). So `oliver.err` is the problems-only signal and
# `oliver.log` is the activity stream (the monitoring skills read accordingly).
_log_fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
_stdout_handler = logging.StreamHandler(sys.stdout)
_stdout_handler.addFilter(lambda r: r.levelno < logging.WARNING)
_stdout_handler.setFormatter(_log_fmt)
_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setLevel(logging.WARNING)
_stderr_handler.setFormatter(_log_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_stdout_handler, _stderr_handler])
log = logging.getLogger("oliver")

# Oliver answers everything in #ask-oliver, but in the main channel he speaks only
# when addressed — @mentioned, called by name, or replied to.
NAME_RE = re.compile(r"\boliver\b", re.IGNORECASE)


def _is_addressed(is_mention: bool, has_name: bool, is_reply_to_bot: bool) -> bool:
    """Whether a main-channel message is directed at Oliver. Pure (testable)."""
    return bool(is_mention or has_name or is_reply_to_bot)


def _channel_mode(cid: int, ask_id: int, monitored_ids: set[int]) -> str:
    """How Oliver treats a channel. Pure (testable).

    "answer"    — respond to everything (the #ask-oliver channel; also the dev
                  fallback when no ask channel is configured).
    "monitored" — passively log every message, reply only when addressed.
    "ignore"    — not Oliver's channel; drop it.
    """
    if not ask_id or cid == ask_id:
        return "answer"
    if cid in monitored_ids:
        return "monitored"
    return "ignore"


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
_email_lock = asyncio.Lock()


def _git_commit_short() -> str:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=publish.REPO_ROOT, capture_output=True, text=True,
            check=False, timeout=5,
        )
        return r.stdout.strip() or "unknown"
    except (subprocess.SubprocessError, OSError):
        return "unknown"


def _activity_text(event: dict) -> str:
    body = (event.get("body") or "").strip()
    title = event["title"].strip()
    prefix = f"**{title}**"
    text = f"{prefix}\n{body}" if body else prefix
    return text[:1900]


def _post_webhook(content: str) -> None:
    if not config.OLIVER_LOG_WEBHOOK_URL:
        raise RuntimeError("DISCORD_OLIVER_LOG_WEBHOOK_URL is not configured")
    r = requests.post(
        config.OLIVER_LOG_WEBHOOK_URL,
        json={"content": content, "username": "Oliver"},
        timeout=15,
    )
    r.raise_for_status()


# Permissions Oliver actually exercises: read+reply in the channel, react with
# ✅ on feedback, embed cover links, and let members invoke /oliver slash cmds.
REQUIRED_PERMS = (
    "view_channel",
    "send_messages",
    "read_message_history",
    "add_reactions",
    "embed_links",
    "use_application_commands",
)


def _missing_permissions(channel: discord.abc.GuildChannel | None) -> list[str]:
    if channel is None or channel.guild is None:
        return []
    perms = channel.permissions_for(channel.guild.me)
    return [name for name in REQUIRED_PERMS if not getattr(perms, name, False)]


@client.event
async def on_ready() -> None:
    # The corpus is a private, gitignored artifact — regenerate it from the DB at startup so
    # on-disk corpus mirrors club_* (nothing else recreates it). Non-fatal: on failure the
    # existing on-disk corpus is the fallback and the bot still connects.
    try:
        written = await asyncio.to_thread(publish.ensure_corpus)
        log.info("corpus regenerated from DB: %s", written)
    except Exception:
        log.exception("startup corpus regen failed (non-fatal); using on-disk corpus")
    log.info("Oliver connected as %s — %d books in the corpus.", client.user, kb.book_count())
    guilds = list(client.guilds)
    log.info("In %d guild(s): %s", len(guilds), ", ".join(g.name for g in guilds) or "(none)")
    ask = client.get_channel(config.ASK_CHANNEL_ID) if config.ASK_CHANNEL_ID else None
    log.info("  ASK_CHANNEL_ID=%s -> %s", config.ASK_CHANNEL_ID, ask)
    monitored = [(cid, client.get_channel(cid)) for cid in sorted(config.MONITORED_CHANNEL_IDS)]
    for cid, ch in monitored:
        log.info("  monitored %s (%s) -> %s", config.CHANNEL_NAMES.get(cid, cid), cid, ch)

    commit = _git_commit_short()
    perm_issues: list[str] = []
    channels_to_check = [("#ask-oliver", ask)] + [
        (config.CHANNEL_NAMES.get(cid, str(cid)), ch) for cid, ch in monitored
    ]
    for label, ch in channels_to_check:
        if ch is None:
            continue
        missing = _missing_permissions(ch)
        if missing:
            perm_issues.append(f"• {label}: missing `{', '.join(missing)}`")
    body = f"Connected as {client.user}; {kb.book_count()} books in corpus; commit `{commit}`."
    if perm_issues:
        body += "\nPermission gaps:\n" + "\n".join(perm_issues)
    db.add_activity("startup", "Oliver online", body)

    commands.start_scheduler()
    start_activity_logger()
    start_email_poller()
    # The member web app is NOT started here — it starts on demand when a member runs
    # /oliver my-club and shuts itself down after idle (see agent/webapp.py).


def start_activity_logger() -> None:
    if not config.OLIVER_LOG_WEBHOOK_URL:
        log.warning("Activity log disabled: configure DISCORD_OLIVER_LOG_WEBHOOK_URL")
        return
    if not post_activity.is_running():
        post_activity.start()


@tasks.loop(seconds=10)
async def post_activity() -> None:
    try:
        events = await asyncio.to_thread(db.pending_activity, limit=10)
    except Exception:
        log.exception("Failed to read pending activity events")
        return
    if not events:
        return
    for event in events:
        text = _activity_text(event)
        try:
            await asyncio.to_thread(_post_webhook, text)
            db.mark_activity_posted(event["id"])
        except Exception as e:
            db.mark_activity_failed(event["id"], f"{type(e).__name__}: {e}")
            log.exception("Failed to post activity event %s", event["id"])
            continue  # don't let one poison event block newer ones (it retries until attempts run out)


@post_activity.before_loop
async def before_post_activity() -> None:
    await client.wait_until_ready()


def start_email_poller() -> None:
    if not email_jmap.enabled():
        log.info("Email disabled: FASTMAIL_JMAP_TOKEN is not configured")
        return
    if not poll_email.is_running():
        poll_email.start()


@tasks.loop(seconds=config.OLIVER_EMAIL_POLL_SECONDS)
async def poll_email() -> None:
    async with _email_lock:
        try:
            messages = await asyncio.to_thread(
                email_jmap.unread_oliver_email,
                limit=config.OLIVER_EMAIL_MAX_PER_POLL,
            )
        except Exception:
            log.exception("Failed to poll Oliver email")
            return
        for msg in messages:
            await _handle_inbound_email(msg)


@poll_email.before_loop
async def before_poll_email() -> None:
    await client.wait_until_ready()


async def _handle_inbound_email(msg: email_jmap.InboundEmail) -> None:
    await inbound_email.handle(msg, schedule_publish=commands.schedule_publish)


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

    mode = _channel_mode(cid, config.ASK_CHANNEL_ID, config.MONITORED_CHANNEL_IDS)
    name_only = False  # name appeared, but no @-mention and not a reply — maybe ABOUT, not TO
    # #ask-oliver: answer everything. (No ask-channel configured → answer everywhere, dev.)
    if mode == "answer":
        question = content.strip()
    # Monitored channels (#general, #book-talk, …): answer only when addressed.
    elif mode == "monitored":
        is_mention = client.user.mentioned_in(message) and not message.mention_everyone
        has_name = bool(NAME_RE.search(content))
        is_reply = await _is_reply_to_bot(message)
        if not _is_addressed(is_mention, has_name, is_reply):
            if content.strip():
                member_slug = db.member_slug_for_user(str(message.author.id))
                db.log_message(str(cid), "user", content.strip(),
                               speaker=message.author.display_name, member_slug=member_slug)
            return
        name_only = has_name and not is_mention and not is_reply
        question = _strip_address(content, client.user.id)
    else:
        return

    if not question:
        return

    async with _channel_locks[message.channel.id]:
        async with message.channel.typing():
            try:
                # A name-only trigger (no @-mention, not a reply) runs through the restraint gate:
                # members often talk ABOUT Oliver to each other, and the model judges whether it
                # was actually addressed — the NO_REPLY sentinel means stay silent. persist=False
                # on this path so the gate note / sentinel never pollute channel memory; we log
                # manually per outcome below.
                prompt = (oliver.PASSING_MENTION_NOTE + question) if name_only else question
                reply = await asyncio.to_thread(
                    oliver.answer, prompt, str(message.channel.id),
                    message.author.display_name, str(message.author.id), str(message.id),
                    medium="discord", persist=not name_only,
                )
            except Exception as e:
                log.exception("Oliver failed to answer")
                # Surface to #oliver-log too — publish/email/archive failures already do, but an
                # interactive answer failure was only hitting stderr, so an admin never saw it.
                db.add_activity(
                    "warning", "Oliver failed to answer a message",
                    f"Channel: {message.channel.id}\nAsker: {message.author.display_name}\n"
                    f"Error: {type(e).__name__}: {e}",
                )
                reply = "Sorry — I hit a snag answering that. Try me again in a moment."

    if name_only:
        if reply.strip().strip("`").startswith(oliver.NO_REPLY_PREFIX):
            # The member was talking about Oliver, not to it — stay silent; keep their message as
            # plain channel context (same as any unaddressed message).
            member_slug = db.member_slug_for_user(str(message.author.id))
            db.log_message(str(cid), "user", content.strip(),
                           speaker=message.author.display_name, member_slug=member_slug)
            return
        # It was a genuine ask after all — persist the exchange (the gated call used
        # persist=False), logging the member's ORIGINAL message, not the gate-note prompt.
        member_slug = db.member_slug_for_user(str(message.author.id))
        db.log_message(str(cid), "user", question,
                       speaker=message.author.display_name, member_slug=member_slug)
        db.log_message(str(cid), "assistant", reply, member_slug=member_slug)

    # Persist the reply intent before crossing Discord's API boundary. message.reply can fail if
    # the original was deleted or permissions changed, so the provider attempt retains the
    # channel.send fallback. An ambiguous provider failure is quarantined by the outbox; replaying
    # this source message then returns the recorded message id instead of posting twice.
    text = reply[:config.MAX_DISCORD_LEN]
    payload = {"channel_id": str(message.channel.id), "content": text,
               "reply_to_message_id": str(message.id)}
    row = outbox.enqueue(
        kind="discord_reply",
        payload=payload,
        idempotency_key=f"discord:reply:{message.id}",
    )

    async def _deliver_reply() -> dict:
        sent: discord.Message
        try:
            sent = await message.reply(text, mention_author=False)
        except discord.HTTPException:
            log.exception(
                "message.reply failed in channel %s; trying channel.send", message.channel.id
            )
            sent = await message.channel.send(text)
        return {"messageId": str(sent.id)}

    delivery: dict | None = None
    try:
        delivery = await outbox.deliver_async(row, _deliver_reply)
    except Exception:
        log.exception("Discord reply delivery failed in %s", message.channel.id)

    # Record what we sent so on_raw_reaction_add can attribute 👍/👎 feedback
    # back to the question that triggered it.
    if delivery and delivery.get("messageId"):
        db.log_response(
            message_id=delivery["messageId"], channel_id=str(message.channel.id),
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


_LOG_DIR = pathlib.Path(__file__).parent / "logs"
_LOG_MAX_BYTES = 5_000_000


def _rotate_launchd_log(name: str, fd: int) -> None:
    """Startup log rotation. launchd appends stdout/stderr to agent/logs/ forever with no
    rotation; at each start, if a file has grown past the cap, keep ONE prior generation (.1)
    and repoint the inherited fd at a fresh file. The dup2 is required — renaming alone would
    leave launchd's open fd writing into the renamed inode. Runs only in main() (never at
    import: tests import this module, and dup2 on fd 1 would hijack pytest's stdout)."""
    path = _LOG_DIR / name
    try:
        if not path.exists() or path.stat().st_size <= _LOG_MAX_BYTES:
            return
        path.replace(path.with_name(name + ".1"))
        fresh = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.dup2(fresh, fd)
        os.close(fresh)
    except OSError:
        pass  # rotation is best-effort; never block startup over it


def main() -> None:
    permission_report = security.enforce_runtime_permissions(repair=True)
    if not permission_report.ok:
        raise SystemExit(
            "Oliver private runtime permissions are unsafe; run "
            "`./agent/script/admin.sh permissions-repair` for details."
        )
    if not config.TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN is not set (add it to the root .env).")
    _rotate_launchd_log("oliver.log", 1)
    _rotate_launchd_log("oliver.err", 2)
    client.run(config.TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
