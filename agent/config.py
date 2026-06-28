"""Env-loaded runtime config shared between bot.py and commands.py.

Lives in its own module so the bot↔commands split doesn't introduce an import
cycle: both sides import from here, neither imports the other for constants.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
ASK_CHANNEL_ID = int(os.environ.get("DISCORD_ASK_OLIVER_CHANNEL_ID") or 0)
ADMIN_USER_ID = int(os.environ.get("DISCORD_ADMIN_USER_ID") or 0)
SERVER_ID = int(os.environ.get("DISCORD_SERVER_ID") or 0)
MAIN_CHANNEL_ID = int(os.environ.get("DISCORD_MAIN_CHANNEL_ID") or 0)
BOOK_TALK_CHANNEL_ID = int(os.environ.get("DISCORD_BOOK_TALK_CHANNEL_ID") or 0)
OLIVER_LOG_WEBHOOK_URL = os.environ.get("DISCORD_OLIVER_LOG_WEBHOOK_URL")

# Channels Oliver monitors passively: he logs every message and replies only when
# addressed (vs. ASK_CHANNEL_ID, where he answers everything). Add a channel here
# and it behaves like #general with no other code change.
MONITORED_CHANNEL_IDS = {cid for cid in (MAIN_CHANNEL_ID, BOOK_TALK_CHANNEL_ID) if cid}

# id → human label, for tagging cross-channel search results to the model.
CHANNEL_NAMES = {
    cid: name
    for cid, name in (
        (ASK_CHANNEL_ID, "#ask-oliver"),
        (MAIN_CHANNEL_ID, "#general"),
        (BOOK_TALK_CHANNEL_ID, "#book-talk"),
    )
    if cid
}

MAX_DISCORD_LEN = 2000
CLUB_TIMEZONE = os.environ.get("CLUB_TIMEZONE", "America/Chicago")

# Member web app (served locally inside the bot process, reached over Tailscale Funnel).
# WEBAPP_BASE_URL is the public Funnel origin (no trailing slash); WEBAPP_PORT is the
# loopback port the in-process aiohttp server binds. Funnel maps the public 443 → this port.
WEBAPP_PORT = int(os.environ.get("WEBAPP_PORT") or 8765)
WEBAPP_BASE_URL = (os.environ.get("WEBAPP_BASE_URL") or "https://otto.tail09aaf9.ts.net").rstrip("/")

# Fastmail/JMAP — optional. If FASTMAIL_JMAP_TOKEN is absent, all email features
# no-op at runtime so local/dev Discord-only runs keep working.
FASTMAIL_JMAP_TOKEN = os.environ.get("FASTMAIL_JMAP_TOKEN")
FASTMAIL_JMAP_SESSION_URL = os.environ.get(
    "FASTMAIL_JMAP_SESSION_URL", "https://api.fastmail.com/jmap/session"
)
OLIVER_EMAIL_ADDRESS = os.environ.get("OLIVER_EMAIL_ADDRESS", "oliver@rwbookclub.com")
BOOK_CLUB_MAILING_LIST_ADDRESS = os.environ.get(
    "BOOK_CLUB_MAILING_LIST_ADDRESS", "rwbookclub@googlegroups.com"
)
# Auto-send the 1-week reminder + 2-day topic email to the whole mailing list at their
# cadence windows. OFF by default — a club-wide blast is consequential, so it must be
# explicitly enabled (CLUB_EMAIL_CADENCE_ENABLED=1) once the drafts are trusted.
CLUB_EMAIL_CADENCE_ENABLED = os.environ.get("CLUB_EMAIL_CADENCE_ENABLED", "0") in {"1", "true", "True"}
OLIVER_EMAIL_INBOX_PARENT = os.environ.get("OLIVER_EMAIL_INBOX_PARENT", "Inbox")
OLIVER_EMAIL_INBOX_FOLDER = os.environ.get("OLIVER_EMAIL_INBOX_FOLDER", "Oliver")
OLIVER_EMAIL_SENT_PARENT = os.environ.get("OLIVER_EMAIL_SENT_PARENT", "Sent")
OLIVER_EMAIL_SENT_FOLDER = os.environ.get("OLIVER_EMAIL_SENT_FOLDER", "Oliver")
OLIVER_EMAIL_POLL_SECONDS = int(os.environ.get("OLIVER_EMAIL_POLL_SECONDS") or 120)
OLIVER_EMAIL_MAX_PER_POLL = int(os.environ.get("OLIVER_EMAIL_MAX_PER_POLL") or 5)
OLIVER_EMAIL_HTML_ENABLED = os.environ.get("OLIVER_EMAIL_HTML_ENABLED", "1") not in {"0", "false", "False"}
