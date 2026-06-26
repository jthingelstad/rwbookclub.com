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
import sys
from collections import defaultdict

import discord
import requests
from discord.ext import tasks

from agent import clubdb, commands, config, context as kb, db, oliver, publish
from agent.mail import email_jmap, email_policy, mail_archive, outbound, tinylytics
from agent.club import meeting_rules

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
ROLL_CALL_SUBJECT_RE = re.compile(r"\broll[- ]?call\b", re.IGNORECASE)
EMAIL_QUOTE_RE = re.compile(r"^(>|on .+wrote:|from:|sent:|to:|subject:|--\s*$)", re.IGNORECASE)
YES_RE = re.compile(r"\b(yes|yep|yeah|sure|attending|i'?ll be there|i can make it|can make it)\b", re.IGNORECASE)
NO_RE = re.compile(r"\b(no|nope|cannot make it|can'?t make it|won'?t make it|not attending|unavailable)\b", re.IGNORECASE)
UNSURE_RE = re.compile(r"\b(unsure|not sure|maybe|tentative|unknown)\b", re.IGNORECASE)


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


def _record_ignored_email(msg: email_jmap.InboundEmail, reason: str) -> None:
    body = (
        f"From: {msg.speaker} <{msg.from_email}>\n"
        f"Subject: {msg.subject or '(no subject)'}\nReason: {reason}"
    )
    db.add_activity("email_ignored", "Email ignored", body)
    log.info("Ignored email %s from %s: %s", msg.id, msg.from_email, reason)


def _first_reply_text(text: str) -> str:
    """Return the member-authored top of an email, excluding quoted history."""
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if lines:
                break
            continue
        if EMAIL_QUOTE_RE.match(line):
            break
        lines.append(line)
        if len(lines) >= 3:
            break
    return " ".join(lines)


def _roll_call_status_from_email(subject: str, text: str) -> str | None:
    """Parse explicit roll-call replies before the model sees the email."""
    if not ROLL_CALL_SUBJECT_RE.search(subject or ""):
        return None
    reply = _first_reply_text(text).replace("’", "'")
    if not reply:
        return None
    if UNSURE_RE.search(reply):
        return "unsure"
    if NO_RE.search(reply):
        return "no"
    if YES_RE.search(reply):
        return "yes"
    return None


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
    start_tinylytics_poller()
    start_email_poller()


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
            return


@post_activity.before_loop
async def before_post_activity() -> None:
    await client.wait_until_ready()


def start_email_poller() -> None:
    if not email_jmap.enabled():
        log.info("Email disabled: FASTMAIL_JMAP_TOKEN is not configured")
        return
    if not poll_email.is_running():
        poll_email.start()


def start_tinylytics_poller() -> None:
    if not tinylytics.enabled():
        log.info("Tinylytics email open sync disabled: configure TINYLYTICS_* variables")
        return
    if not poll_tinylytics.is_running():
        poll_tinylytics.start()


@tasks.loop(seconds=config.TINYLYTICS_SYNC_SECONDS)
async def poll_tinylytics() -> None:
    try:
        synced = await asyncio.to_thread(tinylytics.sync_email_opens)
    except Exception:
        log.exception("Failed to sync Tinylytics email opens")
        return
    if synced:
        db.add_activity(
            "email_opened",
            "Email opens synced",
            f"Recorded {synced} Tinylytics email open(s).",
        )


@poll_tinylytics.before_loop
async def before_poll_tinylytics() -> None:
    await client.wait_until_ready()


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
    if db.email_processed(msg.id):
        return
    if msg.from_email.lower() == config.OLIVER_EMAIL_ADDRESS.lower():
        await asyncio.to_thread(email_jmap.mark_seen, msg.id)
        db.mark_email_processing(
            email_id=msg.id, thread_id=msg.thread_id, from_email=msg.from_email,
            subject=msg.subject, received_at=msg.received_at,
        )
        db.mark_email_processed(msg.id, status="ignored")
        _record_ignored_email(msg, "from_oliver")
        return
    decision = email_policy.inbound_decision(msg)
    if not decision.allowed:
        await asyncio.to_thread(email_jmap.mark_seen, msg.id)
        db.mark_email_processing(
            email_id=msg.id, thread_id=msg.thread_id, from_email=msg.from_email,
            subject=msg.subject, received_at=msg.received_at,
        )
        db.mark_email_processed(msg.id, status="ignored", error=decision.reason)
        _record_ignored_email(msg, decision.reason)
        return
    claimed = db.mark_email_processing(
        email_id=msg.id, thread_id=msg.thread_id, from_email=msg.from_email,
        subject=msg.subject, received_at=msg.received_at,
    )
    if not claimed:
        return
    try:
        await asyncio.to_thread(
            mail_archive.archive_inbound_email,
            msg,
            is_mailing_list=decision.is_mailing_list,
            member_slug=decision.member_slug,
        )
    except Exception as e:
        db.mark_email_processed(msg.id, status="failed", error=f"archive:{type(e).__name__}: {e}")
        log.exception("Failed to archive inbound email %s", msg.id)
        return
    db.add_activity(
        "email_received",
        "Email received",
        f"From: {msg.speaker} <{msg.from_email}>\nSubject: {msg.subject or '(no subject)'}",
    )
    member_slug = decision.member_slug
    member_id = clubdb.lookup_member_id(member_slug)
    recorded_availability: str | None = None
    meeting = meeting_rules.next_meeting() if (member_slug and member_id is not None) else None
    meeting_id = meeting["meetingId"] if meeting else None
    if member_slug and member_id is not None and meeting_id is not None:
        db.add_member_contact(
            meeting_id=meeting_id,
            member_id=member_id,
            kind="email_reply",
            surface="email",
            direction="inbound",
            status="received",
            subject=msg.subject or None,
        )
        recorded_availability = _roll_call_status_from_email(msg.subject, msg.text)
        if recorded_availability:
            db.upsert_roll_call(
                meeting_id=meeting_id,
                channel_id=f"email:{msg.thread_id or msg.from_email.lower()}",
                opened_by="email-reply",
            )
            db.set_attendance(
                meeting_id=meeting_id,
                member_id=member_id,
                status=recorded_availability,
                updated_by_user_id=f"email:{msg.from_email.lower()}",
                source="email",
            )
            db.add_activity(
                "roll_call_update",
                "Roll-call response recorded",
                f"Member: {member_slug}\nStatus: {recorded_availability}\n"
                f"Source: email reply\nMeeting: {meeting['meetingKey']}",
            )
    if decision.is_mailing_list:
        channel_id = f"email:list:{msg.thread_id or config.BOOK_CLUB_MAILING_LIST_ADDRESS.lower()}"
    else:
        channel_id = f"email:{msg.thread_id or msg.from_email.lower()}"
    mailing_list_result: oliver.MailingListEmailResult | None = None
    if decision.is_mailing_list:
        try:
            speaker_user_id = (
                f"member:{member_slug}" if member_slug
                else f"email:{msg.from_email.lower()}"
            )
            mailing_list_result = await asyncio.to_thread(
                oliver.answer_mailing_list_email,
                msg,
                channel_id=channel_id,
                speaker=msg.speaker,
                speaker_user_id=speaker_user_id,
                source_message_id=msg.id,
            )
        except Exception as e:
            db.mark_email_processed(msg.id, status="failed", error=f"{type(e).__name__}: {e}")
            log.exception("Failed to decide whether to reply to mailing-list email %s", msg.id)
            return
        if not mailing_list_result.reply:
            await asyncio.to_thread(email_jmap.mark_seen, msg.id)
            reason = f"mailing_list_model_no_reply:{mailing_list_result.reason or 'no_reason'}"
            db.mark_email_processed(msg.id, status="ignored", error=reason)
            _record_ignored_email(msg, reason)
            return
    runtime_note = ""
    if recorded_availability:
        runtime_note = (
            "[Oliver runtime note: this explicit roll-call reply has already "
            f"been recorded as {recorded_availability} for {member_slug}. "
            "Acknowledge the saved status; do not call record_availability again.]\n\n"
        )
    prompt = runtime_note + (
        f"[Email from {msg.speaker} <{msg.from_email}>]\n"
        f"Subject: {msg.subject or '(no subject)'}\n\n{msg.text}"
    )
    try:
        if mailing_list_result is not None:
            reply = mailing_list_result.body
        else:
            reply = await asyncio.to_thread(
                oliver.answer, prompt, channel_id, msg.speaker, f"email:{msg.from_email.lower()}", msg.id,
            )
        sent = await asyncio.to_thread(
            outbound.send,
            to=decision.reply_to or [msg.from_email],
            subject=msg.reply_subject,
            body=reply,
            in_reply_to=msg.message_id,
            references=msg.references,
        )
        await asyncio.to_thread(email_jmap.mark_seen, msg.id, answered=True)
        db.mark_email_processed(msg.id, reply_email_id=sent.get("emailId"))
        db.add_activity(
            "email_sent",
            "Email reply sent",
            f"To: {msg.from_email}\nSubject: {msg.reply_subject}\nEmail ID: {sent.get('emailId')}",
        )
        log.info("Replied to email %s from %s", msg.id, msg.from_email)
    except Exception as e:
        db.mark_email_processed(msg.id, status="failed", error=f"{type(e).__name__}: {e}")
        log.exception("Failed to handle inbound email %s", msg.id)


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
    # #ask-oliver: answer everything. (No ask-channel configured → answer everywhere, dev.)
    if mode == "answer":
        question = content.strip()
    # Monitored channels (#general, #book-talk, …): answer only when addressed.
    elif mode == "monitored":
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
