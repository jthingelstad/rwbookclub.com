# agent — Oliver

Oliver is the R/W Book Club's Discord agent: a [discord.py](https://discordpy.readthedocs.io/)
bot that answers in **#ask-oliver** as a **tool-using agent** — `claude-opus-4-7`
with adaptive thinking, a manual tool-use loop, prompt caching, **persistent memory**
(SQLite), and per-channel conversation continuity.

```
agent/
├── bot.py          # Discord client: on_ready, #ask-oliver listener, /ping, admin /corpus
├── oliver.py       # the agent loop (tool use, caching, conversation history, usage logging)
├── tools.py        # tool schemas + dispatch (corpus reads + memory)
├── corpus_read.py  # query layer over the per-entity Git corpus
├── context.py      # compact club overview for the cached system prompt
├── db.py           # SQLite memory/state (gitignored)
└── requirements.txt
```

## How Oliver works

- **System prompt** = persona + a compact club overview (`context.py`), cached. Not the
  whole corpus — Oliver pulls specifics on demand via tools.
- **Tools** (`tools.py`): `search_books`, `get_book`, `member_history`, `upcoming_meetings`,
  `club_stats` (read the corpus), plus `remember`, `recall`, `set_reminder` (SQLite).
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
| `DISCORD_ADMIN_USER_ID` | Gates the admin `/corpus` command |
| `ANTHROPIC_API_KEY` | Claude API key |
| `DISCORD_BOT_ID` | The bot's user ID (reference) |
| `OLIVER_DB_PATH` | Optional — SQLite path (default `agent/oliver.db`) |

## Memory & backup

Oliver's memory is a local SQLite file (`agent/oliver.db`, gitignored) — private state
that doesn't belong in the public corpus. On the deployment host, point `OLIVER_DB_PATH`
at durable storage and back it up (litestream or a periodic dump, matching the Weekly
Thing pattern). Backup wiring is a deployment step, not in the repo.

## Discord setup

The bot must be invited with the `bot` + `applications.commands` scopes and have the
**Message Content** privileged intent enabled (Dev Portal → Bot → Privileged Gateway
Intents) — without it `on_message` gets empty content.

## What's next (later phases)

- **Phase 3 — reviews:** a guided review-collection flow that writes finalized reviews to
  the Git corpus (gated, draft → confirm → commit).
- **Phase 4 — meetings & operations:** meeting tools + an in-process scheduler that fires
  the reminders stored here.
- **Phase 5 — "6th member":** persona depth, main-channel presence, milestone awareness.
