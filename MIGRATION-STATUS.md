# Migration Status — SQLite-authoritative club data

Execution record for `MIGRATION-PLAN.md`. Built on branch `sqlite-authoritative` in an
isolated worktree against a consistent snapshot of the live `oliver.db`, so the running
Oliver and the live site were never touched. **Nothing here is live yet** — the cutover is
a short supervised step (below).

> **Later (2026-06-26): corpus is now private + site deploys locally.** Building on the
> SQLite inversion, `corpus/data/` and the machine-generated images are now **gitignored**
> (regenerated from the DB on disk, never committed). The 11ty site is built + deployed
> **locally** by `python -m agent.publish` to the **`gh-pages` branch** (GitHub Pages serves
> it); CI no longer builds the site (`deploy.yml` removed). `main` is pure source — Oliver
> writes nothing to it. Reviews became DB-backed (`club_reviews`) so the startup corpus regen
> can't prune them. See `CLAUDE.md` → "Site build + deploy".

## What's done and verified

| Phase | Status | Evidence |
|---|---|---|
| 1. Authoritative schema (`agent/clubdb.py`) | ✅ | `club_*` tables: integer PKs (Airtable autonumbers; awards minted) + real FKs. No slugs as identity. |
| 2. Airtable → SQLite import (`agent/script/import_airtable.py`) | ✅ | 12 members, 177 authors, 179 books, 184 meetings, 8 reviews, 1 award. IDs + member email/mobile + meeting hosts from Airtable; live scalars + relationships from the corpus. 1 expected warning (dropped Oliver test review). |
| 4. Generators + faithful corpus (`agent/corpus_gen.py`) | ✅ | Regenerated corpus is **byte-identical to the committed corpus except two intentional cleanups**: de-duped `a-distant-mirror`'s `['dan','dan']` picker, dropped `patterns-in-nature--jamie.md`. `corpus/validate.py` green, **240 agent tests pass**, **11ty build renders 553 files**. |
| 5. Writes through the DB (`agent/corpus_write.py`) | ✅ | `write_book` / `schedule_meeting` upsert `club_*` under FKs → regenerate affected corpus files → `gitwrite`. Round-trip tests added. |
| 3. Ops-data remap (verification) | ✅ proven, ⏸ not flipped | `agent/script/verify_ops_mapping.py`: **zero orphans** — every attendance/roll-call/reading/contact/email-tracking row remaps onto a real `meeting_id`/`member_id` FK. The live column rewrite is the supervised step below. |
| 6. Corpus enrichment | 📋 documented | The DB now holds data the corpus never did (e.g. `club_meeting_hosts` — who hosted all 184 meetings) and sits beside the mail archive / Discord history / reading status. Enrichment is additive and trivial from here. See roadmap. |

Design note: the generator **reproduces the existing corpus shape faithfully**, so
`corpus_read.py`, every `website/_data/*.js`, `validate.py`, and the 240 tests keep working
with zero consumer churn. "Better corpus / best intelligence" is delivered *additively*
(Phase 6) rather than by a risky big-bang rewrite of the live read path.

## How to cut over (supervised — ~10 min, reversible)

Run from the **main** working tree once you're watching:

1. **Merge the branch** (no live effect yet — the regenerated corpus is byte-identical
   bar the 2 cleanups, so the public site output is unchanged):
   `git checkout main && git merge sqlite-authoritative`
2. **Seed the live DB** (additive — creates `club_*`, leaves ops tables untouched):
   `python -m agent.script.import_airtable` then `python -m agent.script.verify_ops_mapping`
   (expect zero orphans).
3. **Push** → GitHub Pages redeploys the (effectively identical) site:
   `git push origin main`.
4. **Restart Oliver** so the DB-backed write path is live:
   `launchctl kickstart -k gui/$(id -u)/com.rwbookclub.oliver` (or your usual restart).
5. **Smoke test**: ask Oliver a question in `#ask-oliver` (read path), and optionally run
   `/oliver add-book` on a throwaway then revert — confirm it lands in `club_books` and
   regenerates the corpus file.

A pre-cut snapshot of the live DB is in `agent/backups/` style; the import is wipe-and-reload
on the `club_*` tables only, so re-running it is safe. **Rollback** = `git revert` the merge
+ restart; the `club_*` tables are additive and harmless if left in place.

### Deferred (optional, supervised): ops-layer FK column rewrite

The ops tables still key on `meeting_key` (book slug) / `member_slug`. The remap is proven
(zero orphans) and the FK target tables are designed. Flipping the columns means rewriting
~40 call sites across the live Discord/email/scheduler hot path (inventory in the migration
notes) + a table rebuild + bot restart. The data maps cleanly today, so this is a
future-proofing refactor (clean move/cancel-meeting semantics), **not** required for "SQLite
authoritative." Do it as its own watched change.

## Enrichment roadmap (Phase 6 — additive, no website impact)

The authoritative DB is now the natural home to make Oliver smarter:
- **Meeting hosts** — `club_meeting_hosts` already captures who hosted every meeting (the
  corpus never carried this). Expose via a DB-backed read for member/hosting history.
- **Mailing list / Discord / reading** — `mail_messages` (2,445), `conversations`,
  `reading_statuses` already live in `oliver.db`. Fold summaries into Oliver's context.
- **Book cloud** — see `agent/team/work/2026-06-26-build-book-cloud.md` (slice 1a is
  ready to code); the new schema makes capture/retrieval straightforward.

## Files

New: `agent/clubdb.py`, `agent/corpus_gen.py`, `agent/script/import_airtable.py`,
`agent/script/verify_ops_mapping.py`, `tests/test_clubdb_writes.py`.
Changed: `agent/corpus_write.py` (DB-backed), `corpus/data/*` (2 cleanups), `.gitignore`
(import cache w/ member PII), `tests/test_write_paths.py` (normalized author fixture).
