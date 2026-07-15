# agent — Oliver

Oliver is the R/W Book Club's Discord agent: a [discord.py](https://discordpy.readthedocs.io/)
bot that answers in **#ask-oliver** as a **tool-using agent** (`claude-sonnet-5` for replies
and history summaries, with Opus reserved for one-off quality-critical generation) — a manual
tool-use loop, prompt caching, **persistent memory** (SQLite), conversation continuity, and a
proactive scheduler.

```
agent/
├── bot.py          # Discord client: /oliver group, #ask-oliver listener, scheduler loop
├── oliver.py       # the agent loop (tool use, caching, conversation history)
├── tools.py        # stable schemas + the single registry/authorization/dispatch gate
├── tool_handlers/  # capability handlers: meeting, memory/retrieval, mail, Book Cloud/picking
├── model_readers.py # actor-required private readers (separate from raw internal DB APIs)
├── clubdb.py       # the authoritative club_* tables + read/write helpers
├── corpus_gen.py   # generate the (private, gitignored) corpus from the DB
├── corpus_read.py  # query/join layer over the generated corpus
├── corpus_write.py # web-app write path: club_* DB upsert → corpus regen + validate
├── webapp/         # member + admin web editor (aiohttp + Jinja2, Tailscale Funnel, token→cookie auth)
├── publish.py      # local build + deploy of the site to the gh-pages branch
├── scheduler.py    # pure due_notifications (reminders / nudges / milestones)
├── context.py      # compact club overview for the cached system prompt
├── persona.py      # loads the SOUL/PURPOSE/PROCESS charters from docs/
├── config.py · db.py   # config; SQLite façade + ordered schema migrations (gitignored DB)
├── repositories/      # focused persistence for outbox delivery and scheduled jobs
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
- **Tools** (`tools.py` + `tool_handlers/`): `find_books`, `search_books`, `get_book`, `member_history`,
  `upcoming_meetings`, `club_stats`, `pending_reviews`, `club_lists` (read the corpus), club-awareness
  tools (`horizon` for the read-only five-book runway, `current_club_state`,
  `current_meeting_status`, `identity_status`,
  `recent_feedback`, `recent_channel_context`), relationship tools (`related_books`,
  `compare_books`, `review_summary`), email (`send_email`, `email_status`), proposal staging
  (`propose_action`, `open_proposals`), the Book Cloud (`book_cloud_add`, `book_cloud_recent` —
  books members reference but haven't read, reason-first, private SQLite), the picking funnel
  (`pick_prospects` → `pick_fit` — discover then evaluate pick candidates against member tastes,
  shelf-neighbor verdicts, and the cloud), plus `remember`, `recall`, `set_reminder`, and explicit
  self-reported `record_availability` (SQLite).
- **Tool privacy is enforced by the dispatcher and row-level readers, not by prompt trust.** Public
  corpus tools are open; club-operational/private tools require a linked member; admin repair tools
  are admin-only. Members can search shared Discord/mailing-list history plus their own 1:1
  conversations and memories, never another member's private state. Pick dossiers use another
  member's public picks/reviews but omit their exact private memory notes. The dispatcher resolves
  one typed `RequestContext` from trusted runtime identity and passes it into capability-scoped
  handlers; model input cannot supply the actor. Private handlers read through `model_readers.py`,
  whose APIs require an actor, while raw `db.py` readers remain an internal/admin repository surface.
- **Reviews / ratings / lists / profile**: members manage these in the **web app** (`/oliver my-club`,
  see below), which writes `club_reviews` / `club_lists` / `member_identities` and regenerates the
  corpus. Identity comes from the Discord-user → member map (carried into a signed web session), not
  mutable display names.
- **Operations** (admin), organized into `/oliver` subcommand groups: `/oliver meeting`
  roll-call/dashboard/check-in; `/oliver admin` (status, feedback, proposals, resolve,
  release-notes, link-member, reattribute-mail, tick). Admin **book/meeting/member editing and
  memory grooming** live in the web app. Writes go through `corpus_write.py` and validate the
  corpus before commit.
- **Feedback** (any member): react 👍 or 👎 to any of Oliver's replies. The bot logs the
  reaction (user, message, question that prompted it) to SQLite and confirms with ✅. Use
  `/oliver admin feedback` for a quick summary plus the most recent 👍/👎 with context.
- **Meeting roll call** (`meeting_rules.py` + `commands.py`): Oliver knows the club's standing
  mechanics — last Tuesday of the month, quorum is 3 of 5 current members, and the picker must
  attend. He can open roll call with buttons, record explicit self-reported availability, show
  status, email roll-call prompts to linked member addresses, and flag quorum/picker trouble.
  Replies from linked email addresses update the same attendance tracker as Discord. He does
  **not** cancel, reschedule, or change reading order. Attendance/reading state lives in the
  event-sourced `events` log + `meeting_member_status` projection (see `docs/ERD.md` §2), so once a
  member's attendance or reading is confirmed Oliver stops asking.
- **Proactive scheduler** (`scheduler.py` + `commands.py`): a daily loop posts
  upcoming-meeting reminders, starts roll call 14 days before the next meeting, flags
  unresolved attendance trouble within 3 days, posts review nudges, and milestone/anniversary
  notes to `DISCORD_MAIN_CHANNEL_ID` — deduped, and a no-op until that channel id is set.
  Once a member confirms attendance, Oliver may email reading-status check-ins at most three
  times before the meeting: first in the 14-day window, second in the 7-day window, and final
  in the 2-day window, with at least two days between automated asks. A persistent renewable
  lease prevents overlapping hourly/manual ticks; recurring components also record start/end,
  outcome, duration, processed count, and privacy-safe error class in `job_runs`. Inspect current
  owners, expiry, last success/failure, and overdue state with `python -m agent.jobs status`.
- **Email** (`email_jmap.py` + `bot.py`): optional Fastmail JMAP integration. Oliver sends HTML
  plus plain-text alternative mail from
  `OLIVER_EMAIL_ADDRESS`, polls only `Inbox/Oliver` for unread mail, replies through the normal
  `oliver.answer` path, stores sent messages in `Sent/Oliver`, marks handled inbound mail seen,
  and dedupes processed inbound email ids in SQLite. Oliver does not track email opens (no pixel) —
  member privacy. Per-member sends are recorded as `attendance_requested`/`reading_requested` events
  (bumping the projection's ask counts) for the campaign dashboard; nothing records whether a member
  read an email.
- **Durable outbound delivery** (`outbox.py` + `db.py`): member-facing email and Discord intents
  are committed to `outbox_messages` before a provider call. Stable keys make replay idempotent;
  known pre-provider failures retry with bounded backoff, while an interrupted or ambiguous
  in-flight provider attempt is quarantined as `uncertain` rather than risking a duplicate. The
  weekly health email reports pending, retrying, uncertain, and dead counts.
- **Meeting campaign** (`meeting_campaign.py` + `/oliver meeting dashboard`): combines the
  current book/date, days remaining, roll call, picker requirement, reading status, per-member ask
  counts + last-asked timestamps, and recommended next actions into one dashboard/tool snapshot.
- **Activity log** (`db.py` + `bot.py`): startup, email, reading-progress, roll-call, reminder,
  and scheduler activity is queued in SQLite and posted to `#oliver-log` through
  `DISCORD_OLIVER_LOG_WEBHOOK_URL`. Startup no longer posts to `#ask-oliver`.
- **Reading progress** (`db.py` + `tools.py` + `/oliver reading status`): tracks each current
  member's status for the next scheduled book from the corpus. Updates can come from linked
  Discord users or linked email addresses; `/oliver meeting check-in` emails a member for an update.
  Oliver skips members already marked `finished`.
- **Memory** (`db.py`): durable notes with provenance, per-channel conversation log + rolling
  summary, reminders, usage log, feedback, and private identity links. Survives restarts.
- **Speaker** is matched from the linked Discord user ID first, with display-name fallback only
  for conversational personalization.

## Run it

Long-running process — **not** GitHub Pages/Actions (that's the website). Run on an
always-on host (VPS, Fly.io, home server). Locally:

```bash
python3.13 -m venv venv
venv/bin/pip install -c agent/constraints.txt -r agent/requirements.txt
venv/bin/python -m agent.bot                 # run from the repo root
```

Run from the **repo root** so the `agent` and `corpus` packages resolve.

## Configuration (root `.env`)

| Variable | Purpose |
|---|---|
| `DISCORD_BOT_TOKEN` | The bot's login token (Dev Portal → your app → Bot → Reset Token) |
| `DISCORD_ASK_OLIVER_CHANNEL_ID` | Only messages in this channel get answered |
| `DISCORD_MAIN_CHANNEL_ID` | Main channel: scheduler posts here, and Oliver replies here when addressed (no-op if unset) |
| `DISCORD_ADMIN_USER_ID` | Gates the admin `/oliver` commands (stats, add-book, release-notes, tick) |
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
| `DISCORD_OLIVER_LOG_WEBHOOK_URL` | Optional — webhook for `#oliver-log` operational activity |

## Memory & backup

Oliver's memory is a local SQLite file (`agent/oliver.db`, gitignored) — private state
that doesn't belong in the public corpus. On the deployment host, point `OLIVER_DB_PATH`
at durable storage and back it up (litestream or a periodic dump, matching the Weekly
Thing pattern). Oliver enforces owner-only modes for `.env`, SQLite and its sidecars,
`agent/logs/`, `agent/backups/`, the generated private corpus, and offsite backups. Audit with
`./agent/script/admin.sh permissions`; repair safe mode drift with `permissions-repair`.

### Command structure

`/oliver` is organized into subcommand groups by purpose (Discord's 2-level nesting:
`/oliver <group> <subcommand> [options]`). Quick top-level commands: `/oliver ping`, `/oliver whoami`,
`/oliver my-club` (see "Member + admin web app" below), and `/oliver private-feedback book:`
for a private, member-scoped note about a club book. Private feedback is retained only in Oliver's
memory for future pick/recommendation context; it never changes a public review or the website.

Structured, public, deliberate editing — **book ratings/reviews, lists, profile/contact, and admin
data management** — moved to the web app. Discord stays primary for the conversational, private,
in-the-moment things (attendance, reading status, private meeting feedback).

- **`/oliver reading`** (members) — `status [status|progress|page|percent]` shows everyone / updates
  yours. (Reviews moved to the web app.)
- **`/oliver meeting`** (admin) — `dashboard` readiness; `roll-call action:<status|start|remind|email|close>`
  runs attendance (email roll call targets pending members only); `check-in member:<slug>` emails a
  reading nudge.
- **`/oliver timeline`** — `show [member|category]` (members) views the club event log; `log date:
  category: text: [member]` (admin) records an event.
- **`/oliver admin`** — `status` (release/models/data/memory card), `feedback`, `proposals`,
  `resolve id: decision:<accept|dismiss>`, `release-notes [to]`, `link-member member: user:`
  (needs Discord's user picker — the one identity op still in Discord), `reattribute-mail`,
  `tick` (run the scheduler now).
- Retired in the 2026-07 command review (webapp owns them now): the `contact` group
  (email/SMS linking + audit → member editor + Members-page coverage badges), the `memory`
  group (→ admin Memories page), and `library add-book` (→ admin Books → Add).

## Member + admin web app (`/oliver my-club`)

A real web editor served **inside the bot process** (`agent/webapp/`, aiohttp + Jinja2) and reached
over **Tailscale Funnel**. `/oliver my-club` mints a single-use token (the Discord identity link *is*
the auth); the member opens the URL, the server exchanges the token for a signed session cookie, and
they edit on a page. The server starts on demand and idles off after ~15 min; changes go live on a
**Publish** button or on idle shutdown (deferred publish — no per-write rebuild).

- **Member tabs:** Ratings (one-click 1–5/DNF across all books), Reviews (per-book, Markdown body),
  Lists (CRUD), Profile (websites/emails/phones).
- **Admin tabs:** Books (edit core fields; title is read-only to keep the slug), Meetings (add + edit,
  mark held), Hosts.
- **Writers are reused** — `reviews.write_review`, `agent/club/lists.py`, `db.link_member_*`,
  `clubdb.upsert_book`, and the new `clubdb.set_rating`/`update_meeting`/`set_meeting_hosts`. Auth/
  session/CSRF live in `agent/webapp/sessions.py`; routes in `routes_member.py`/`routes_admin.py`.

## Tests

```bash
venv/bin/pip install -c agent/constraints.txt -r tests/requirements.txt
venv/bin/pytest tests/
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

- **Presence tuning**: optional unprompted chime-ins and finer name-matching — once addressed-only
  presence has run for a while.
- **Semantic retrieval**: embeddings over reviews, meeting notes, and related materials.
