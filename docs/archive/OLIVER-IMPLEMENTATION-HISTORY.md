# Oliver implementation history (archived)

> Historical record only. These phases describe how Oliver was built and include superseded
> paths and commands. GitHub Issues are the current roadmap; use `CLAUDE.md` and
> `agent/README.md` for maintained architecture and capability guidance.

Oliver is the R/W Book Club's Discord agent. The goal: a real agent — tool use +
memory — that is **authoritative** on the club's reading history, **collects book
reviews**, **manages meetings and club operations**, and feels like a **6th
member**, not a foreign bot.

## Architecture: split data by class, not by store

> **Update (2026-06-26): the data model was inverted.** SQLite (the `club_*` tables in
> `agent/oliver.db`) is now **authoritative**; the corpus in `corpus/data/` is **generated**
> from it and is **private/gitignored**; the site builds + deploys **locally** to the
> `gh-pages` branch (`main` is pure source — Oliver writes nothing to it). The table below is
> updated; the phase history further down predates this. See `docs/archive/MIGRATION-PLAN.md`
> and `docs/archive/MIGRATION-STATUS.md` for the inversion.

| Class | Examples | Home |
|---|---|---|
| **A — Canonical club knowledge** | books, authors, meetings, members, reviews, lists | **SQLite** `club_*` tables (authoritative); the corpus in `corpus/data/` is generated from them (private/gitignored), read by Oliver + the website build |
| **B — Oliver's private memory/state** | conversation summaries, member taste notes, Discord identity links, reminders, usage/cost, the mail archive | **SQLite** on Oliver's host (gitignored, backed up) |
| **C — write flow** | a review, a scheduled meeting | web app (or admin Discord cmd) → DB upsert under FKs → corpus regenerated → local build + deploy to `gh-pages` |

Consequences: the DB is the single source of truth; the static site is built + deployed
locally from the DB-generated corpus (CI only runs tests). Oliver is a **self-hosted Claude
API agentic loop** (`claude-sonnet-5`, prompt caching, adaptive thinking) with a **manual
tool-use loop** so irreversible/outward actions are gated. Hosting + ops reuse the Weekly
Thing pattern (process supervisor, SQLite backup, host `.env`).

## Phases

**Phase 1 — Git as the source of truth.** ✅ Done at the time — **later superseded by the
SQLite inversion (Phase 6); SQLite is now authoritative and the corpus is generated/gitignored.**
Migrated the corpus from Airtable fetch-artifact to per-entity text files
(`corpus/data/{books,members,meetings,authors,reviews,lists}/`), records as JSON
and reviews as Markdown+frontmatter, each keyed by its Airtable `rec…` id;
meetings are now first-class. Website data layer globs + aggregates them
(byte-identical output, verified). Covers come from Open Library (`corpus/images.py`,
idempotent) instead of expiring Airtable URLs. Airtable retired to a read-only
cold backup; `refresh.yml` removed; CLAUDE.md + memories updated to Git-canonical.

**Phase 2 — Oliver's spine (agent loop + memory).** ✅ **Done.**
`agent/oliver.py` is a manual tool-use loop with prompt caching, per-channel
conversation history, rolling summaries, and usage logging. SQLite class-B state
holds durable memories with provenance, reminders, conversation summaries,
feedback, usage logs, scheduler dedup, and private Discord identity links. Tools:
read/authority (`find_books`, `search_books`, `get_book`, `member_history`,
`get_author`, `club_lists`, `upcoming_meetings`, `club_stats`, `pending_reviews`)
plus awareness and local-state tools (`current_club_state`, `current_meeting_status`,
`identity_status`, `recent_feedback`, `recent_channel_context`), richer corpus relationship
tools (`related_books`, `compare_books`, `review_summary`), proposal-staging tools
(`propose_action`, `open_proposals`), and memory/reminder tools (`remember`, `recall`,
`set_reminder`, `record_availability`).

**Phase 3 — Reviews (the wedge; exercises the write flow).** ✅ **Done.**
Members submit reviews through a Discord modal. Oliver resolves the member through
the private Discord-user → member identity map, upserts `club_reviews`
(`clubdb.upsert_review`), regenerates the corpus review file, and the site is rebuilt +
deployed by the publish step. Submitting the modal is the confirmation.

**Phase 4 — Meetings & operations.** ✅ **Done.**
`/oliver library add-book` (fetches metadata + cover from Open Library, writes a book file) and
`/oliver library schedule` (book + date + picker → writes a placeholder meeting + sets the book's
picker), admin-gated, via `agent/corpus_write.py` (both since retired — the web app's admin
Books/Meetings pages own these writes as of the 2026-07 command review); writes go to the `club_*` DB, regenerate
the corpus, validate it, and create missing author records for new books. An in-process
`discord.ext.tasks` loop (`agent/scheduler.py`, pure `due_notifications`) posts proactive
upcoming-meeting reminders, a review nudge for the most-recent read, and milestone/anniversary
notes to `DISCORD_MAIN_CHANNEL_ID` — deduped via a `notifications_sent` table; `/oliver tick`
runs it on demand. (Also shipped along the way: a 3.5 pass that normalized the corpus and
stripped the Airtable cruft, plus consolidating the slash commands under `/oliver`.)

Meeting schedule authority stays deliberately human: the club normally meets on the last
Tuesday of the month, needs 3 of 5 current members for quorum, and the picker must attend.
Oliver supports that with roll call rather than scheduling autonomy: `/oliver roll-call
start|status|remind|close`, persistent attendance buttons, explicit self-reported
availability via chat, automatic roll-call posting within 10 days, and an attendance warning
within 3 days when quorum or picker attendance is not confirmed.

For club operations Oliver should not perform directly, he can now stage proposals in SQLite
for admin review (`/oliver admin proposals`, `/oliver admin resolve`) instead of pretending a
suggested action is already approved.

**Phase 5 — "6th member" polish.** ✅ **Done.**
Oliver is now present in the main channel, not just `#ask-oliver`: he answers there only when
addressed — @mentioned, called "Oliver" by name, or replied to (`bot.py` `_is_addressed` /
`_strip_address`; unaddressed main-channel messages are logged as passive context, and each
channel keeps its own conversation thread + rolling summary serialized through a per-channel
lock). The persona was deepened to read like a long-time member
(real opinions, group-channel etiquette, no help-desk tone) and `_question_block` now injects
both the speaker's remembered tastes and club-scoped lore, so replies personalize. Admin memory
commands (`/oliver memory search`, `edit`, `forget`) provide a repair path for bad durable
notes (since retired — the web app's admin Memories page is the repair path as of 2026-07). Milestone/anniversary celebration shipped in Phase 4's scheduler.
Awards were **superseded by book lists** (Phase 7) — `club_awards` was retired and the one
record (2016 Book of the Year) migrated into a "Books of the Year" club list.

**Phase 6 — SQLite inversion + corpus enrichment.** ✅ **Done** (2026-06-26; see
`docs/archive/MIGRATION-*`). SQLite became authoritative (`club_*` tables, integer PKs + FKs);
the corpus is generated from it and made private/gitignored; the site builds + deploys locally
to `gh-pages`. Enrichment added: external book/author data (Open Library + Wikidata + Wikipedia,
`python -m agent.enrich`) into 1:1 sidecar tables, and **hosting history** surfaced
(`member_history`/`club_stats`/`get_book`/`club_context` + member pages). The email/Discord
archive stays **tool-accessed** (`search_mail_archive`/`get_mail_thread`/`search_discussion`),
deliberately not folded into the corpus, keeping private message bodies out of it.

**Phase 7 — Book lists.** ✅ **Done.** Replaced `club_awards` with a member/club book-list system
(`club_lists` + `club_list_books`, ordered entries with optional per-book notes). Members create and
manage their own lists ("my favorites"); admins curate club lists ("our favorite books"). Each list
has a description. Originally managed via a `/oliver list` Discord group — **now managed in the web
app** (Lists tab; the Discord group was retired in Phase 8). Lists render on member profiles and on
their own `/lists/<slug>/` pages; club lists
get a **Lists** nav hub at `/lists/`. Read surface: the `club_lists` tool + member lists via
`member_history`. The one legacy award (2016 Book of the Year) migrated into a "Books of the Year"
club list. (The separate enrichment-driven literary-awards field on books is unrelated and stays.)

**Phase 8 — Member + admin web app.** ✅ **Done.** A real web editor served **inside the bot process**
(`agent/webapp/`, aiohttp + Jinja2) over **Tailscale Funnel**, authed by a Discord-minted single-use
token → signed session cookie (the Discord identity link *is* the login; no passwords, no cloud DB —
the authoritative SQLite stays local and the Mac dials out via Funnel). On-demand lifecycle (starts on
`/oliver my-club`, idles off ~15 min) and **deferred publish** (Publish button / idle shutdown, no
per-write rebuild). Member tabs: bulk **ratings** grid (1–5/DNF, one click), **reviews** (Markdown),
**lists**, **profile/contact**. Admin tabs: **book** data, **meetings** (add/edit, mark held),
**hosts**. Reuses existing writers + new `clubdb.set_rating`/`update_meeting`/`set_meeting_hosts`. The
obsoleted Discord commands were retired (`/oliver reading review`, the `/oliver list` group, member
self-service `/oliver contact` add/remove, `/oliver library schedule`); their writers stay. Division
of labor: Discord keeps attendance, reading status, and **private meeting feedback**; the web app owns
the structured/public editing. Deferred: enrichment review, member name/bio/photo, book title rename.

**What's next (candidates).** The "book cloud" capture stream
(`agent/team/work/2026-06-26-build-book-cloud.md`, slice 1a ready); backfilling the 3
picker-less / host-less book-meetings (Love Sense, Complexity, Being Mortal) once the names are known.

## Cross-cutting (from Phase 2 on)

- **Write path:** Oliver upserts the `club_*` DB under FKs → regenerates the affected corpus
  files → validates → schedules a background publish that rebuilds the site and deploys it to the
  `gh-pages` branch. (Originally this committed the corpus to `main` and let CI deploy; the corpus
  is now private/gitignored and the build/deploy is local.)
- **Guardrails:** promote every irreversible/outward action (commit/push, post, DM)
  to a dedicated, confirmable tool; admin-gate club-wide ops.
- **Hosting/ops:** Weekly Thing pattern; SQLite backup (litestream or periodic dump).
- **Iteration:** guild-scoped slash-command sync (`DISCORD_SERVER_ID`) for instant updates.
- **Cost/observability:** log usage per turn; keep prompt caching on the system +
  corpus + tool-definition prefix.
- **Evaluation:** `tests/eval.py` mixes generated questions with golden Discord-style
  conversations for grounding, tone, identity, memory, and multi-turn context.
