# agent — Oliver

Oliver is the R/W Book Club's Discord agent: a [discord.py](https://discordpy.readthedocs.io/)
bot that answers in **#ask-oliver** as a **tool-using agent** (`claude-sonnet-4-6`, with
Haiku for cheap summaries and Opus reserved) — a manual tool-use loop, prompt caching,
**persistent memory** (SQLite), conversation continuity, and a proactive scheduler.

```
agent/
├── bot.py          # Discord client: /oliver group, #ask-oliver listener, scheduler loop
├── oliver.py       # the agent loop (tool use, caching, conversation history)
├── tools.py        # read/memory/email tool schemas + dispatch
├── clubdb.py       # the authoritative club_* tables + read/write helpers
├── corpus_gen.py   # generate the (private, gitignored) corpus from the DB
├── corpus_read.py  # query/join layer over the generated corpus
├── corpus_write.py # /oliver add-book & schedule → DB upsert + corpus regen
├── publish.py      # local build + deploy of the site to the gh-pages branch
├── scheduler.py    # pure due_notifications (reminders / nudges / milestones)
├── context.py      # compact club overview for the cached system prompt
├── persona.py      # loads the SOUL/PURPOSE/PROCESS charters from docs/
├── config.py · db.py   # config; SQLite memory/state (gitignored)
├── club/           # reviews (→ club_reviews), meeting_rules, openlibrary, campaign/emails
├── mail/           # Fastmail JMAP send/receive + the mail archive
├── enrich/         # external enrichment loop (Open Library + Wikidata + Wikipedia)
├── script/         # one-off ops: import_airtable, dump_club_seed, prune_backups, verify_ops_mapping
└── docs/ · team/   # charters + roadmap · AI-persona role prompts
```

## How Oliver works

- **System prompt** = persona + a compact club overview (`context.py`), cached. Not the
  whole corpus — Oliver pulls specifics on demand via tools.
- **Presence**: answers everything in `#ask-oliver`; in `DISCORD_MAIN_CHANNEL_ID` he speaks
  only when addressed — @mentioned, called "Oliver" by name, or replied to (`bot.py`
  `_is_addressed`). Unaddressed main-channel messages are still logged as passive context,
  so the next addressed reply can account for what happened in the room. Each channel keeps
  its own conversation thread + rolling summary, and messages are answered through a
  per-channel queue so group chat stays in order.
- **Tools** (`tools.py`): `find_books`, `search_books`, `get_book`, `member_history`,
  `upcoming_meetings`, `club_stats`, `pending_reviews` (read the corpus), club-awareness
  tools (`current_club_state`, `current_meeting_status`, `identity_status`,
  `recent_feedback`, `recent_channel_context`), relationship tools (`related_books`,
  `compare_books`, `review_summary`), email (`send_email`, `email_status`), proposal staging
  (`propose_action`, `open_proposals`), plus `remember`, `recall`, `set_reminder`, and explicit
  self-reported `record_availability` (SQLite).
- **Reviews** (`/oliver review`): members log reviews via a Discord form that writes
  `club_reviews` (DB-backed) and regenerates the corpus review file — see below. Review
  identity comes from the private Discord-user → member map, not mutable display names.
- **Operations** (admin, `/oliver`): `add-book` (Open Library → book file + cover), `schedule`
  (book + date + picker → a meeting), `roll-call start/remind/close`, `link-member`,
  `identities`, `stats`, `tick`, `feedback`, and memory maintenance. Writes go through
  `corpus_write.py` and validate the corpus before commit.
- **Feedback** (any member): react 👍 or 👎 to any of Oliver's replies. The bot logs the
  reaction (user, message, question that prompted it) to SQLite and confirms with ✅. Use
  `/oliver feedback` (admin) for a quick summary plus the most recent 👍/👎 with context.
- **Meeting roll call** (`meeting_rules.py` + `commands.py`): Oliver knows the club's standing
  mechanics — last Tuesday of the month, quorum is 3 of 5 current members, and the picker must
  attend. He can open roll call with buttons, record explicit self-reported availability, show
  status, email roll-call prompts to linked member addresses, and flag quorum/picker trouble.
  Replies from linked email addresses update the same attendance tracker as Discord. He does
  **not** cancel, reschedule, or change reading order.
- **Proactive scheduler** (`scheduler.py` + `commands.py`): a daily loop posts
  upcoming-meeting reminders, starts roll call 14 days before the next meeting, flags
  unresolved attendance trouble within 3 days, posts review nudges, and milestone/anniversary
  notes to `DISCORD_MAIN_CHANNEL_ID` — deduped, and a no-op until that channel id is set.
  Once a member confirms attendance, Oliver may email reading-status check-ins at most three
  times before the meeting: first in the 14-day window, second in the 7-day window, and final
  in the 2-day window, with at least two days between automated asks.
- **Email** (`email_jmap.py` + `bot.py`): optional Fastmail JMAP integration. Oliver sends HTML
  plus plain-text alternative mail from
  `OLIVER_EMAIL_ADDRESS`, polls only `Inbox/Oliver` for unread mail, replies through the normal
  `oliver.answer` path, stores sent messages in `Sent/Oliver`, marks handled inbound mail seen,
  and dedupes processed inbound email ids in SQLite. If `TINYLYTICS_SITE_ID`,
  `TINYLYTICS_SITE_ID_NUMERIC`, and `TINYLYTICS_API_KEY` are configured, operational emails
  include a Tinylytics open pixel and a visible disclosure note; Oliver polls Tinylytics and
  records observed opens in SQLite.
- **Meeting campaign** (`meeting_campaign.py` + `/oliver meeting-dashboard`): combines the
  current book/date, days remaining, roll call, picker requirement, reading status, last member
  contact, email opens, and recommended next actions into one dashboard/tool snapshot.
- **Activity log** (`db.py` + `bot.py`): startup, email, reading-progress, roll-call, reminder,
  and scheduler activity is queued in SQLite and posted to `#oliver-log` through
  `DISCORD_OLIVER_LOG_WEBHOOK_URL`. Startup no longer posts to `#ask-oliver`.
- **Reading progress** (`db.py` + `tools.py` + `/oliver reading-status`): tracks each current
  member's status for the next scheduled book from the corpus. Updates can come from linked
  Discord users or linked email addresses; `/oliver reading-checkin` emails a member for an update.
  Oliver skips members already marked `finished`.
- **Memory** (`db.py`): durable notes with provenance, per-channel conversation log + rolling
  summary, reminders, usage log, feedback, and private identity links. Survives restarts.
- **Speaker** is matched from the linked Discord user ID first, with display-name fallback only
  for conversational personalization.

## Run it

Long-running process — **not** GitHub Pages/Actions (that's the website). Run on an
always-on host (VPS, Fly.io, home server). Locally:

```bash
pip install -r agent/requirements.txt        # also pulls in corpus deps
python -m agent.bot                            # run from the repo root
```

Run from the **repo root** so the `agent` and `corpus` packages resolve.

## Configuration (root `.env`)

| Variable | Purpose |
|---|---|
| `DISCORD_BOT_TOKEN` | The bot's login token (Dev Portal → your app → Bot → Reset Token) |
| `DISCORD_ASK_OLIVER_CHANNEL_ID` | Only messages in this channel get answered |
| `DISCORD_MAIN_CHANNEL_ID` | Main channel: scheduler posts here, and Oliver replies here when addressed (no-op if unset) |
| `DISCORD_ADMIN_USER_ID` | Gates the admin `/oliver` commands (stats, add-book, schedule, tick) |
| `DISCORD_SERVER_ID` | Guild for instant (guild-scoped) slash-command sync |
| `ANTHROPIC_API_KEY` | Claude API key |
| `DISCORD_BOT_ID` | The bot's user ID (reference) |
| `OLIVER_DB_PATH` | Optional — SQLite path (default `agent/oliver.db`) |
| `OLIVER_CORPUS_DIR` | Optional — corpus dir override (tests redirect to a temp dir) |
| `OLIVER_ENRICH_ON_WRITE` | Optional — set to `0` to skip inline enrichment on add-book (tests) |
| `FASTMAIL_JMAP_TOKEN` | Optional — Fastmail API token for Oliver email |
| `OLIVER_EMAIL_ADDRESS` | Optional — defaults to `oliver@rwbookclub.com` |
| `OLIVER_EMAIL_INBOX_PARENT` / `OLIVER_EMAIL_INBOX_FOLDER` | Optional — defaults to `Inbox` / `Oliver`; only this folder is polled |
| `OLIVER_EMAIL_SENT_PARENT` / `OLIVER_EMAIL_SENT_FOLDER` | Optional — defaults to `Sent` / `Oliver`; sent mail is moved here |
| `OLIVER_EMAIL_POLL_SECONDS` | Optional — defaults to `120` |
| `OLIVER_EMAIL_HTML_ENABLED` | Optional — defaults to `1`; sends HTML plus plain-text alternative mail |
| `TINYLYTICS_SITE_ID` / `TINYLYTICS_SITE_ID_NUMERIC` | Optional — Tinylytics site identifiers for email open pixels/API reads |
| `TINYLYTICS_API_KEY` | Optional — read-only Tinylytics API key for syncing observed email opens |
| `TINYLYTICS_SYNC_SECONDS` | Optional — defaults to `600`; Tinylytics email-open polling interval |
| `DISCORD_OLIVER_LOG_WEBHOOK_URL` | Optional — webhook for `#oliver-log` operational activity |

## Memory & backup

Oliver's memory is a local SQLite file (`agent/oliver.db`, gitignored) — private state
that doesn't belong in the public corpus. On the deployment host, point `OLIVER_DB_PATH`
at durable storage and back it up (litestream or a periodic dump, matching the Weekly
Thing pattern). Backup wiring is a deployment step, not in the repo.

Admins can inspect and repair private state in Discord:

- `/oliver link-member member:<slug> user:<Discord user>` links a stable Discord identity
- `/oliver link-email member:<slug> email:<address>` links a stable email identity
- `/oliver identities` shows current identity links
- `/oliver reading-status [status] [progress] [page] [percent]` shows or updates the next-book tracker
- `/oliver reading-checkin member:<slug>` emails a member for a next-book progress update
- `/oliver memories [subject] [query]` searches durable memories
- `/oliver edit-memory` and `/oliver forget` curate incorrect or stale memories
- `/oliver roll-call status` shows the current attendance/quorum/picker check
- `/oliver roll-call start|remind|email|close` runs the attendance flow in Discord or email.
  Email roll call targets pending members only.
- `/oliver meeting-dashboard` shows readiness, last contact/open state, and next actions
- `/oliver proposals` and `/oliver resolve-proposal` review staged Oliver suggestions

## Reviews (`/oliver review`)

Members log book reviews with the `/oliver review` command: pick the book (autocomplete), fill the
form (rating 1–5 or DNF, the review, recommend?, discussion quality, favorite quote), and
submit. Oliver upserts the review into `club_reviews` (the authoritative DB) and regenerates the
corpus review file (`corpus/data/reviews/<book>--<member>.md`); the command then schedules a
background publish that rebuilds + deploys the site to `gh-pages`. Only linked club members can
submit, and submitting the form is the confirmation. `reviews.py` → `clubdb.upsert_review` is the
single write path (any future front-end reuses it).

## Tests

```bash
pip install -r tests/requirements.txt    # one-time
pytest tests/                             # ~290 tests
```

Pure helpers (`_is_addressed`, `_strip_address`, rating parsers, `parse_frontmatter`,
`scheduler.due_notifications`, `meeting_rules`, richer corpus relationship tools,
`find_books` scoring, `books()` cache, dispatch error paths, db round-trips) all locked in.
Tests use a scratch SQLite DB and a temp corpus dir (`tests/conftest.py` sets `OLIVER_DB_PATH`
and `OLIVER_CORPUS_DIR` before any agent module imports), seed `club_*` from the public-safe
`tests/fixtures/club_seed.sql`, and never touch the live state. An autouse fixture stubs
`publish.publish_site`, and `OLIVER_ENRICH_ON_WRITE=0` keeps add-book offline — so no test builds,
deploys, or hits the network.

For behavioral quality, `python -m tests.eval --round N --note "..."` runs Oliver through
generated plus golden Discord-style conversations and judges tool choice, grounding, tone,
identity, memory use, and multi-turn context. It writes rounds to `agent/logs/oliver-eval-log.md` (gitignored).

## Discord setup

The bot must be invited with the `bot` + `applications.commands` scopes and have the
**Message Content** privileged intent enabled (Dev Portal → Bot → Privileged Gateway
Intents) — without it `on_message` gets empty content.

## What's next (later phases)

- **Awards facilitation** (deferred from Phase 5): a way to record awards to the corpus, and
  maybe a Discord voting flow. The corpus, site rendering, and a sample record already exist.
- **Presence tuning**: optional unprompted chime-ins and finer name-matching — once addressed-only
  presence has run for a while.
- **Semantic retrieval**: embeddings over reviews, meeting notes, and related materials.
