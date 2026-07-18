# AGENT-TEAM/ — Oliver build team

Role-prompts, each meant to run as a **scheduled Codex/Claude agent** that maintains and
improves Oliver (the R/W Book Club's resident agent — librarian, memory keeper, meeting aide,
de facto sixth member). Each file is a self-contained job: a lane, a boundary, an "Every run"
runbook, and a success definition. Point a scheduled agent at one file and let it run.

**The workflow these roles share** — the GitHub-Issues spine, the approval gate, the label
taxonomy, `wip` claiming, commit lanes, the `notes/` convention, and the operating rules — is
defined once in **[`WORKFLOW.md`](WORKFLOW.md)** and is identical across all of Jamie's
projects. This README covers only what's specific to Oliver. Every role reads `AGENTS.md` →
`WORKFLOW.md` → this file → its role file, then acts.

```
AGENT-TEAM/
  WORKFLOW.md          # the shared contract (identical across projects)
  README.md            # this file — Oliver specifics
  <role>.md            # the roster below
  scripts/             # setup-labels.sh · preflight.sh · queue-audit.sh · new-note.sh
  notes/               # gitignored per-run scratch
  summaries/           # committed weekly Manager digests
  work/                # committed durable design docs / briefs, linked from issues
```

## The team

| Role | File | Lane | Commits? |
|------|------|------|----------|
| Product Manager | `product-manager.md` | Discovers what's worth building (the approval gate) | No — issue-only |
| Build Manager | `build-manager.md` | Works the backlog into tested changes | **Yes — owns feature/bug code** |
| Evaluator | `evaluator.md` | Rubrics, golden conversations, regression tests | Yes — evals & tests only |
| Operations Manager | `operations-manager.md` | Bot health + site publish/deploy | Yes — ops fixes + deploys |
| Manager | `manager.md` | Weekly meta-review of the team itself | Own `summaries/` only |
| Club Ethnographer | `club-ethnographer.md` | Club culture, member taste, tone, book-judgment | No — issue-only |

Product Manager, Build Manager, Evaluator, Operations Manager, and Manager are the standard
**core** (shared across projects). **Club Ethnographer** is Oliver's domain role — the club's
counterpart to Elixir's Data Analyst. Commit lanes and the approval gate are in `WORKFLOW.md`.

## Shared context (read first, every run)

Before role-specific work, every agent reads: `agent/docs/SOUL.md` (who Oliver is),
`agent/docs/PURPOSE.md` (what Oliver is for), `agent/docs/PROCESS.md` (how Oliver operates,
incl. the member-communication cadence), and `CLAUDE.md` (architecture, schema, build/deploy,
the full "things not to do"). GitHub Issues are the current roadmap; completed design records
live under `AGENT-TEAM/work/` and `docs/archive/`.

## Guardrails (Oliver specifics)

- **`club_*` SQLite is the canonical club record.** The corpus (`corpus/data/`) is generated
  from it and gitignored — never hand-edit the corpus; change data via Oliver's validated
  writers, then regen.
- Oliver's private SQLite is operational state. Jamie authorizes schedule changes and
  non-review corpus writes; all members may submit their own reviews.
- **Member communications are high-trust.** Oliver may DM/email individuals for nudges and send
  club-wide meeting comms only under the approved cadence in `PROCESS.md`. Never treat a member
  blast as an operational fix. Optimize for *this* club — not generic chatbot helpfulness.

## Deploy

The bot runs under launchd (`com.rwbookclub.oliver`). The public site is built + force-pushed to
`gh-pages` by `uv run --locked python -m agent.publish` (regen corpus → `npm run build` → deploy). Both are the
**Operations Manager's** to run; the Build Manager commits code and hands deploy/restart off via
the issue.

## Design docs vs. notes

`AGENT-TEAM/work/` holds committed durable artifacts (product briefs, build plans, eval plans,
ethnography baselines), named `<issue>-<role>.md` and linked from the issue that owns them. That
is separate from `AGENT-TEAM/notes/` — gitignored per-run scratch (see `WORKFLOW.md`). Durable =
issues + `work/` + the Manager's `summaries/`; ephemeral = `notes/`.

## Suggested cadence

Recommended defaults — the actual scheduling lives in Codex/Claude routines. All times
America/Chicago.

| Role | Cadence |
|------|---------|
| Operations Manager | Hourly (or every few hours) — bot + site health |
| Build Manager | Daily — backlog burn-down |
| Evaluator | Weekly + after any behavior/prompt/workflow change |
| Product Manager | Weekly — discovery |
| Club Ethnographer | Weekly (or when a change touches tone/memory/selection) |
| Manager | Weekly — team-health review + notes digest |

## North star

The goal is **not "more automation"** — it is better book club conversation: stronger picks,
better meeting readiness, more useful memory, and discussion prompts that reflect the club's real
reading history. Oliver should feel like a useful sixth member, grounded in the corpus.
