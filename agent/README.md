# agent — Oliver

Oliver is the R/W Book Club's Discord agent: a [discord.py](https://discordpy.readthedocs.io/)
bot that answers questions in the **#ask-oliver** channel by sending them to
Claude (`claude-opus-4-7`) with the [knowledge corpus](../corpus/) as context.

```
agent/
├── bot.py        # Discord client: on_ready, #ask-oliver listener, /ping, admin /corpus
├── oliver.py     # the Claude call (Anthropic SDK, prompt-cached corpus block)
├── context.py    # reads corpus/data and builds Oliver's background digest
└── requirements.txt
```

## Run it

Oliver is a long-running process — it does **not** run on GitHub Pages/Actions
(that's the website's deploy path). Run it on an always-on host (a small VPS,
Fly.io, a home server, etc.). Locally:

```bash
pip install -r agent/requirements.txt        # also pulls in corpus deps
python -m agent.bot                            # run from the repo root
```

Run from the **repo root** so the `agent` and `corpus` packages both resolve.

## Configuration (root `.env`)

| Variable | Purpose |
|---|---|
| `DISCORD_BOT_TOKEN` | The bot's login token (Dev Portal → your app → Bot → Reset Token) |
| `DISCORD_ASK_OLIVER_CHANNEL_ID` | Only messages in this channel get answered |
| `DISCORD_ADMIN_USER_ID` | Gates the admin `/corpus` command |
| `ANTHROPIC_API_KEY` | Claude API key |
| `DISCORD_BOT_ID` | The bot's user ID (reference) |

## Discord setup

The bot must be invited to the server and have the **Message Content** privileged
intent enabled (Dev Portal → Bot → Privileged Gateway Intents) — without it
`on_message` receives empty content. Invite with the `bot` and
`applications.commands` scopes so slash commands (`/ping`, `/corpus`) register.

## What's stubbed for later

This is a working end-to-end loop (Discord → corpus context → Claude → reply),
intentionally minimal. Next-phase TODOs live in `oliver.py`: tools (live
Airtable lookups, writing reviews back to the base), a retrieval strategy for
when the corpus outgrows a single cached prompt, and system-prompt tuning.
