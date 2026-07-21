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

# Public site origin (the gh-pages deploy target, served by GitHub Pages). Used by the
# self-healing publish check to read the deployed /next.json marker. No trailing slash.
SITE_URL = (os.environ.get("SITE_URL") or "https://rwbookclub.com").rstrip("/")

# Member web app (served locally inside the bot process, reached over Tailscale Funnel).
# WEBAPP_BASE_URL is the public Funnel origin (no trailing slash); WEBAPP_PORT is the
# loopback port the in-process aiohttp server binds. Funnel maps the public 443 → this port.
WEBAPP_PORT = int(os.environ.get("WEBAPP_PORT") or 8765)
WEBAPP_BASE_URL = (os.environ.get("WEBAPP_BASE_URL") or "https://otto.tail09aaf9.ts.net").rstrip(
    "/"
)
# Secret for signing web-app session cookies + CSRF tokens. This must be independent from every
# service credential: compromising a session-signing key must not also compromise the Discord bot.
# The dev literal is ONLY for local/test use — the web-app server refuses to bind with it
# (agent/webapp/server.py:ensure_running), so a misconfigured production process fails closed.
WEBAPP_DEV_SECRET = "insecure-dev-secret"
WEBAPP_SECRET = os.environ.get("WEBAPP_SECRET") or WEBAPP_DEV_SECRET

# Fastmail/JMAP — optional. If FASTMAIL_JMAP_TOKEN is absent, all email features
# no-op at runtime so local/dev Discord-only runs keep working.
FASTMAIL_JMAP_TOKEN = os.environ.get("FASTMAIL_JMAP_TOKEN")
FASTMAIL_JMAP_SESSION_URL = os.environ.get(
    "FASTMAIL_JMAP_SESSION_URL", "https://api.fastmail.com/jmap/session"
)
OLIVER_EMAIL_ADDRESS = os.environ.get("OLIVER_EMAIL_ADDRESS", "oliver@rwbookclub.com")
# Review drive: member-slug allowlist for the weekly review-request emails ("all" = every
# current member; empty = feature off). Member-facing automation is opt-in in every environment.
REVIEW_DRIVE_MEMBERS = os.environ.get("OLIVER_REVIEW_DRIVE_MEMBERS", "")

# Daily enrichment sweep: enrich new books/authors, retry incomplete ones (capped, flagged).
ENRICH_SWEEP_ENABLED = os.environ.get("OLIVER_ENRICH_SWEEP_ENABLED", "1") not in {
    "0",
    "false",
    "False",
}
ENRICH_SWEEP_LIMIT = int(os.environ.get("OLIVER_ENRICH_SWEEP_LIMIT") or 8)

# Weekly health digest email to the admin. The rule is inverted alarming: Oliver writes every
# week, so a MISSING digest is itself the signal that something is wrong.
HEALTH_DIGEST_ENABLED = os.environ.get("OLIVER_HEALTH_DIGEST_ENABLED", "1") not in {
    "0",
    "false",
    "False",
}
HEALTH_DIGEST_WEEKDAY = int(os.environ.get("OLIVER_HEALTH_DIGEST_WEEKDAY") or 0)  # Monday
HEALTH_DIGEST_HOUR = int(os.environ.get("OLIVER_HEALTH_DIGEST_HOUR") or 8)  # 8am club time

# Daily off-machine DB backup → iCloud Drive (agent/backup.py; iCloud syncs it off the Mac).
# ON by default; the directory default is the Mac's iCloud Drive root + Oliver/backups.
OFFSITE_BACKUP_ENABLED = os.environ.get("OLIVER_OFFSITE_BACKUP_ENABLED", "1") not in {
    "0",
    "false",
    "False",
}
OFFSITE_BACKUP_DIR = os.environ.get(
    "OLIVER_OFFSITE_BACKUP_DIR",
    "~/Library/Mobile Documents/com~apple~CloudDocs/Oliver/backups",
)
OFFSITE_BACKUP_KEEP = int(os.environ.get("OLIVER_OFFSITE_BACKUP_KEEP") or 14)

# Oliver's own club_members row (the "sixth member": public profile, webapp login). Human-only
# mechanics — roll calls, check-ins, outreach, audits, taste lenses — must exclude this slug
# (use corpus_read.human_current_members / filter on it), or Oliver starts emailing himself.
OLIVER_MEMBER_SLUG = "oliver"
BOOK_CLUB_MAILING_LIST_ADDRESS = os.environ.get(
    "BOOK_CLUB_MAILING_LIST_ADDRESS", "rwbookclub@googlegroups.com"
)
# Auto-send the 1-week reminder + 2-day topic email to the whole mailing list at their
# cadence windows. OFF by default — a club-wide blast is consequential, so it must be
# explicitly enabled (CLUB_EMAIL_CADENCE_ENABLED=1) once the drafts are trusted.
CLUB_EMAIL_CADENCE_ENABLED = os.environ.get("CLUB_EMAIL_CADENCE_ENABLED", "0") in {
    "1",
    "true",
    "True",
}
# Auto-send "Postscript" — the ~1-week-AFTER-meeting digest of real recent news about books the
# club has read. OFF by default and independent of the pre-meeting cadence above: it's a new,
# experimental club-wide blast, so trial it self-only (/oliver postscript) until the drafts are
# trusted, then enable (CLUB_POSTSCRIPT_ENABLED=1).
CLUB_POSTSCRIPT_ENABLED = os.environ.get("CLUB_POSTSCRIPT_ENABLED", "0") in {"1", "true", "True"}
# Weekly reflective-memory pass (Sunday early morning): distills recent conversations into durable
# member memories. Internal — no member-facing output; audited in #oliver-log and inspectable via
# the web app admin Memories page. ON by default; set OLIVER_REFLECTION_ENABLED=0 to disable.
OLIVER_REFLECTION_ENABLED = os.environ.get("OLIVER_REFLECTION_ENABLED", "1") not in {
    "0",
    "false",
    "False",
}
OLIVER_EMAIL_INBOX_PARENT = os.environ.get("OLIVER_EMAIL_INBOX_PARENT", "Inbox")
OLIVER_EMAIL_INBOX_FOLDER = os.environ.get("OLIVER_EMAIL_INBOX_FOLDER", "Oliver-In")
OLIVER_EMAIL_SENT_PARENT = os.environ.get("OLIVER_EMAIL_SENT_PARENT", "Sent")
OLIVER_EMAIL_SENT_FOLDER = os.environ.get("OLIVER_EMAIL_SENT_FOLDER", "Oliver-Sent")
OLIVER_EMAIL_POLL_SECONDS = int(os.environ.get("OLIVER_EMAIL_POLL_SECONDS") or 120)
OLIVER_EMAIL_MAX_PER_POLL = int(os.environ.get("OLIVER_EMAIL_MAX_PER_POLL") or 5)
OLIVER_EMAIL_HTML_ENABLED = os.environ.get("OLIVER_EMAIL_HTML_ENABLED", "1") not in {
    "0",
    "false",
    "False",
}
