# Oliver roadmap: from stub bot to the club's 6th member

Oliver is the R/W Book Club's Discord agent. The goal: a real agent — tool use +
memory — that is **authoritative** on the club's reading history, **collects book
reviews**, **manages meetings and club operations**, and feels like a **6th
member**, not a foreign bot.

## Architecture: split data by class, not by store

| Class | Examples | Home |
|---|---|---|
| **A — Canonical club knowledge** | books, authors, meetings, members, finalized reviews, awards | **Git**, as per-entity text files in `corpus/data/` (source of truth) |
| **B — Oliver's private memory/state** | conversation summaries, member taste notes, draft reviews, reminders, cost | **SQLite** on Oliver's host (gitignored, backed up) |
| **C — B→A flow** | a review being collected | SQLite draft → committed to Git on confirm |

Consequences: git history is the club's operational audit log; the static site
builds from committed text; member contribution is enabled for free (the
contributable surface — corpus + code — is already text-in-Git). Oliver is a
**self-hosted Claude API agentic loop** (`claude-opus-4-7`, prompt caching,
adaptive thinking) with a **manual tool-use loop** so irreversible/outward
actions are gated. Hosting + ops reuse the Weekly Thing pattern (process
supervisor, SQLite backup, host `.env`).

## Phases

**Phase 1 — Git as the source of truth.** ✅ **Done.**
Migrated the corpus from Airtable fetch-artifact to per-entity text files
(`corpus/data/{books,members,meetings,authors,reviews,awards}/`), records as JSON
and reviews as Markdown+frontmatter, each keyed by its Airtable `rec…` id;
meetings are now first-class. Website data layer globs + aggregates them
(byte-identical output, verified). Covers come from Open Library (`corpus/images.py`,
idempotent) instead of expiring Airtable URLs. Airtable retired to a read-only
cold backup; `refresh.yml` removed; CLAUDE.md + memories updated to Git-canonical.

**Phase 2 — Oliver's spine (agent loop + memory).** ✅ **Done.**
Convert `agent/oliver.py` from one-shot to a manual tool-use loop. Stand up
SQLite (schema + migrations + backup) for class B: `memories`, `member_state`,
`reminders`, `review_drafts`, `conversations`, `cost_log`. Tools: read/authority
(`search_books`, `get_book`, `member_history`, `find_reviews`, `upcoming_meetings`,
`club_stats`) over the per-entity corpus; memory (`remember`, `recall`,
`set_reminder`). Per-channel conversation history + rolling summary.

**Phase 3 — Reviews (the wedge; exercises B→A).** ✅ **Done.**
Guided review flow in DM/thread: draft in SQLite → on confirm, write
`reviews/<book>--<member>.md`, commit, push → site shows it. Proactive nudges
("you attended the Caste meeting but haven't reviewed it"). Write tools gated:
draft shown for confirmation before commit.

**Phase 4 — Meetings & operations.** ✅ **Done.**
`/oliver add-book` (fetches metadata + cover from Open Library, writes a book file) and
`/oliver schedule` (book + date + picker → writes a placeholder meeting + sets the book's
picker), admin-gated, via `agent/corpus_write.py` + `agent/gitwrite.py`. An in-process
`discord.ext.tasks` loop (`agent/scheduler.py`, pure `due_notifications`) posts proactive
upcoming-meeting reminders, a review nudge for the most-recent read, and milestone/anniversary
notes to `DISCORD_MAIN_CHANNEL_ID` — deduped via a `notifications_sent` table; `/oliver tick`
runs it on demand. (Also shipped along the way: a 3.5 pass that normalized the corpus and
stripped the Airtable cruft, plus consolidating the slash commands under `/oliver`.)

**Phase 5 — "6th member" polish.**
Persona grounded in the 24-year history and each member's tastes; presence in the
main channel (not just `#ask-oliver`); memory-driven personalization; awards
facilitation; milestone/anniversary celebration.

## Cross-cutting (from Phase 2 on)

- **Git write path:** Oliver pulls → writes file(s) → commits with a descriptive
  message → pushes to `main` (auto-deploys); handle the rare push race with
  pull/rebase + retry.
- **Guardrails:** promote every irreversible/outward action (commit/push, post, DM)
  to a dedicated, confirmable tool; admin-gate club-wide ops.
- **Hosting/ops:** Weekly Thing pattern; SQLite backup (litestream or periodic dump).
- **Iteration:** guild-scoped slash-command sync (`DISCORD_SERVER_ID`) for instant updates.
- **Cost/observability:** log usage per turn; keep prompt caching on the system +
  corpus + tool-definition prefix.
