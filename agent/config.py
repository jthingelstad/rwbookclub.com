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
