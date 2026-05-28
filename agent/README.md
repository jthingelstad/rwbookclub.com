# agent — Oliver

Oliver is the R/W Book Club's Discord agent: a [discord.py](https://discordpy.readthedocs.io/)
bot that answers in **#ask-oliver** as a **tool-using agent** (`claude-sonnet-4-6`, with
Haiku for cheap summaries and Opus reserved) — a manual tool-use loop, prompt caching,
**persistent memory** (SQLite), conversation continuity, and a proactive scheduler.

```
agent/
├── bot.py          # Discord client: /oliver group, #ask-oliver listener, scheduler loop
├── oliver.py       # the agent loop (tool use, caching, conversation history)
├── tools.py        # read/memory tool schemas + dispatch
├── corpus_read.py  # query/join layer over the normalized Git corpus
├── corpus_write.py # write books + meetings (add-book / schedule) via gitwrite
├── reviews.py      # write reviews   (gitwrite.py = commit/push corpus changes)
├── scheduler.py    # pure due_notifications (reminders / nudges / milestones)
├── openlibrary.py  # metadata lookup for add-book
├── context.py      # compact club overview for the cached system prompt
├── db.py           # SQLite memory/state + scheduler dedup (gitignored)
└── requirements.txt
```

## How Oliver works

- **System prompt** = persona + a compact club overview (`context.py`), cached. Not the
  whole corpus — Oliver pulls specifics on demand via tools.
- **Presence**: answers everything in `#ask-oliver`; in `DISCORD_MAIN_CHANNEL_ID` he speaks
  only when addressed — @mentioned, called "Oliver" by name, or replied to (`bot.py`
  `_is_addressed`). Each channel keeps its own conversation thread + rolling summary.
- **Tools** (`tools.py`): `search_books`, `get_book`, `member_history`, `upcoming_meetings`,
  `club_stats`, `pending_reviews` (read the corpus), plus `remember`, `recall`, `set_reminder`
  (SQLite).
- **Reviews** (`/oliver review`): members log reviews via a Discord form that writes to the
  Git corpus (`reviews.py` → `gitwrite.py`) — see below.
- **Operations** (admin, `/oliver`): `add-book` (Open Library → book file + cover), `schedule`
  (book + date + picker → a meeting), `stats`, `tick`, `feedback` (see below). Writes go
  through `corpus_write.py`.
- **Feedback** (any member): react 👍 or 👎 to any of Oliver's replies. The bot logs the
  reaction (user, message, question that prompted it) to SQLite and confirms with ✅. Use
  `/oliver feedback` (admin) for a quick summary plus the most recent 👍/👎 with context.
- **Proactive scheduler** (`scheduler.py`): a daily loop posts upcoming-meeting reminders, a
  review nudge, and milestone/anniversary notes to `DISCORD_MAIN_CHANNEL_ID` — deduped, and a
  no-op until that channel id is set.
- **Memory** (`db.py`): durable notes, per-channel conversation log + rolling summary,
  reminders, and a usage log. Survives restarts.
- **Speaker** is matched from the Discord display name to a club member (best-effort) so
  Oliver can personalize and attribute reviews.

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
| `OLIVER_GIT_PUSH` | Optional — set to `0` to commit corpus writes locally without pushing |

## Memory & backup

Oliver's memory is a local SQLite file (`agent/oliver.db`, gitignored) — private state
that doesn't belong in the public corpus. On the deployment host, point `OLIVER_DB_PATH`
at durable storage and back it up (litestream or a periodic dump, matching the Weekly
Thing pattern). Backup wiring is a deployment step, not in the repo.

## Reviews (`/oliver review`)

Members log book reviews with the `/oliver review` command: pick the book (autocomplete), fill the
form (rating 1–5 or DNF, the review, recommend?, discussion quality, favorite quote), and
submit. Oliver writes `corpus/data/reviews/<book>--<member>.md`, commits, and pushes to
`main` — live after the deploy. Only recognized club members can submit, and submitting the
form is the confirmation. `reviews.py` → `gitwrite.py` is the single write path (any future
front-end reuses it). Set `OLIVER_GIT_PUSH=0` to commit locally without pushing (dev).

## Discord setup

The bot must be invited with the `bot` + `applications.commands` scopes and have the
**Message Content** privileged intent enabled (Dev Portal → Bot → Privileged Gateway
Intents) — without it `on_message` gets empty content.

## What's next (later phases)

- **Awards facilitation** (deferred from Phase 5): a way to record awards to the corpus, and
  maybe a Discord voting flow. The corpus, site rendering, and a sample record already exist.
- **Presence tuning**: optional unprompted chime-ins and finer name-matching — once addressed-only
  presence has run for a while.
