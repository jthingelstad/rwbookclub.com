# AGENTS.md — rwbookclub.com

**Read [`CLAUDE.md`](CLAUDE.md) — it is the canonical, maintained guide for this repo**
(architecture, schema, build/deploy, gotchas, and the full "things not to do"). This file is a
thin pointer so the two don't drift; everything below lives in `CLAUDE.md` in full.

## Orientation

A monorepo for the R/W Book Club: the **`agent/`** Discord agent "Oliver" (Python), the
**`corpus/`** knowledge layer (Python), and the **`website/`** static site (11ty/Node).
**SQLite (`agent/oliver.db`, the `club_*` tables) is the source of truth**; the corpus
(`corpus/data/`) is generated from it and is **private/gitignored**; the site builds + deploys
**locally** to the `gh-pages` branch (`main` is pure source — CI only runs tests). All Python
runs from the repo root (`python -m agent.bot`, `python -m agent.publish`, `python -m agent.corpus_gen`).
History of the SQLite inversion lives in `docs/archive/MIGRATION-*`.

## Non-negotiables (full list + rationale in `CLAUDE.md` → "Things not to do")

- The `club_*` SQLite tables are authoritative — change data via Oliver's write tools / the DB,
  then regenerate; **don't hand-edit `corpus/data/`** (a regen clobbers it). Airtable is retired —
  don't reintroduce it as a live dependency.
- Never commit member PII (emails/mobiles); `oliver.db` and the Airtable import cache are gitignored.
- Don't fetch metadata from Google Books (quota exhausted) — use Open Library.
- Don't commit `.env` or hard-code the PAT / bot token / API key.

## Work Tracking

**GitHub Issues on this repository are the canonical work queue** for the agent team. Default:
open an issue before non-trivial work. Commit directly to `main` — PRs are not required —
referencing the issue (`Closes #N`) so GitHub auto-closes on push. All times **America/Chicago**.

The agent team, its roles, and the full workflow live in **[`AGENT-TEAM/`](AGENT-TEAM/)**:
[`AGENT-TEAM/WORKFLOW.md`](AGENT-TEAM/WORKFLOW.md) is the shared contract (label taxonomy, the
`proposal → approved → build+deploy` gate, `wip` claiming, commit lanes, the gitignored `notes/`
convention); [`AGENT-TEAM/README.md`](AGENT-TEAM/README.md) is the Oliver-specific overview. New
direction is gated on Jamie's approval; defects (`bug`/`regression`/`operations`) flow straight
through. Run `AGENT-TEAM/scripts/setup-labels.sh` once to create the labels.
