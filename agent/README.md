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
├── meeting_rules.py # last-Tuesday roll call, quorum, picker-attendance checks
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
  `_is_addressed`). Unaddressed main-channel messages are still logged as passive context,
  so the next addressed reply can account for what happened in the room. Each channel keeps
  its own conversation thread + rolling summary, and messages are answered through a
  per-channel queue so group chat stays in order.
- **Tools** (`tools.py`): `find_books`, `search_books`, `get_book`, `member_history`,
  `upcoming_meetings`, `club_stats`, `pending_reviews` (read the corpus), club-awareness
  tools (`current_club_state`, `current_meeting_status`, `identity_status`,
  `recent_feedback`, `recent_channel_context`), relationship tools (`related_books`,
  `compare_books`, `review_summary`), proposal staging (`propose_action`, `open_proposals`),
  plus `remember`, `recall`, `set_reminder`, and explicit self-reported `record_availability`
  (SQLite).
- **Reviews** (`/oliver review`): members log reviews via a Discord form that writes to the
  Git corpus (`reviews.py` → `gitwrite.py`) — see below. Review identity comes from the
  private Discord-user → member map, not mutable display names.
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
  status, and flag quorum/picker trouble. He does **not** cancel, reschedule, or change reading
  order.
- **Proactive scheduler** (`scheduler.py` + `commands.py`): a daily loop posts
  upcoming-meeting reminders, starts roll call within 10 days of the next meeting, flags
  unresolved attendance trouble within 3 days, posts review nudges, and milestone/anniversary
  notes to `DISCORD_MAIN_CHANNEL_ID` — deduped, and a no-op until that channel id is set.
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
| `OLIVER_GIT_PUSH` | Optional — set to `0` to commit corpus writes locally without pushing |

## Memory & backup

Oliver's memory is a local SQLite file (`agent/oliver.db`, gitignored) — private state
that doesn't belong in the public corpus. On the deployment host, point `OLIVER_DB_PATH`
at durable storage and back it up (litestream or a periodic dump, matching the Weekly
Thing pattern). Backup wiring is a deployment step, not in the repo.

Admins can inspect and repair private state in Discord:

- `/oliver link-member member:<slug> user:<Discord user>` links a stable Discord identity
- `/oliver identities` shows current identity links
- `/oliver memories [subject] [query]` searches durable memories
- `/oliver edit-memory` and `/oliver forget` curate incorrect or stale memories
- `/oliver roll-call status` shows the current attendance/quorum/picker check
- `/oliver roll-call start|remind|close` runs the attendance flow in Discord
- `/oliver proposals` and `/oliver resolve-proposal` review staged Oliver suggestions

## Reviews (`/oliver review`)

Members log book reviews with the `/oliver review` command: pick the book (autocomplete), fill the
form (rating 1–5 or DNF, the review, recommend?, discussion quality, favorite quote), and
submit. Oliver writes `corpus/data/reviews/<book>--<member>.md`, commits, and pushes to
`main` — live after the deploy. Only linked club members can submit, and submitting the
form is the confirmation. `reviews.py` → `gitwrite.py` is the single write path (any future
front-end reuses it). Writes are rolled back locally if corpus validation fails before commit.
Set `OLIVER_GIT_PUSH=0` to commit locally without pushing (dev).

## Tests

```bash
pip install -r tests/requirements.txt    # one-time
pytest tests/                             # 112 tests, ~0.6s
```

Pure helpers (`_is_addressed`, `_strip_address`, rating parsers, `parse_frontmatter`,
`scheduler.due_notifications`, `meeting_rules`, richer corpus relationship tools,
`find_books` scoring, `books()` cache, dispatch error paths, db round-trips) all locked in.
Tests use a scratch SQLite DB (`tests/conftest.py`
sets `OLIVER_DB_PATH` before any agent module imports) and never touch the live state.
`OLIVER_GIT_PUSH=0` + `OLIVER_GIT_DRYRUN=1` are set by the conftest as belt-and-suspenders
against any accidental git activity.

For behavioral quality, `python -m tests.eval --round N --note "..."` runs Oliver through
generated plus golden Discord-style conversations and judges tool choice, grounding, tone,
identity, memory use, and multi-turn context. It writes rounds to `oliver-test-log.md`.

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
