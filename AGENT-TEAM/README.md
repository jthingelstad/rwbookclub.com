# AGENT-TEAM/ — Oliver build team

Role prompts for the agents that maintain and improve Oliver (the R/W Book Club's resident agent —
librarian, memory keeper, meeting aide, de facto sixth member). Run them in a **normal Codex project
session**, except for the few activities whose inputs are inherently calendar-based. Each file is a
self-contained job: a lane, a boundary, an "Every run" runbook, and a success definition.

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
| Evaluator | `evaluator.md` | Read-only Discord/email audits, rubrics, goldens, regressions | Yes — evals & tests only |
| Operations Manager | `operations-manager.md` | Bot health + site publish/deploy | Yes — ops fixes + deploys |
| Manager | `manager.md` | Weekly meta-review of the team itself | Own `summaries/` only |
| Club Ethnographer | `club-ethnographer.md` | Club culture, member taste, tone, book-judgment | No — issue-only |

Product Manager, Build Manager, Evaluator, Operations Manager, and Manager are the standard
**core** (shared across projects). **Club Ethnographer** is Oliver's domain role — the club's
counterpart to Elixir's Data Analyst. Commit lanes and the approval gate are in `WORKFLOW.md`.

## Activity-driven dispatch

GitHub issue state, not a role's calendar, drives executable handoffs. Run the deterministic
selector with `AGENT-TEAM/scripts/dispatcher-admin.sh shadow`, claim the selected issue, then start
the role from an active Codex conversation as a normal local thread in the `rwbookclub.com`
project. The local launchd watcher is intentionally uninstalled: shell-launched `codex exec` runs
are persisted on disk but do not appear as normal project threads, and the app-owned thread
creation tool is not available to launchd.

| Handoff label | Worker |
|---------------|--------|
| `dispatch:build` | Build Manager |
| `dispatch:operations` | Operations Manager |
| `dispatch:evaluator` | Evaluator |
| `dispatch:culture` | Club Ethnographer |
| `dispatch:product` | Product Manager |
| `dispatch:manager` | Manager |

Exactly one handoff label is active at a time. `needs-eval` and `needs-culture` remember downstream
acceptance still owed while `needs-deploy` retains its existing operational meaning. A dispatched
role accepts the dispatcher's `wip` claim, removes its current handoff before finishing, and either
closes the issue, puts it in an explicit human stop state (`proposal`, `blocked`, `needs-design`),
or adds exactly one next handoff. Product proposals still wait for Jamie; defects and approved work
may flow autonomously.

Each on-demand role runs preflight, owns the shared checkout for its turn, and leaves the next
handoff in GitHub. Threads use compact live titles such as `#79 Eval · evidence` and
`#79 Eval · tests`, then settle at `#79 Eval ✓` after a valid transition or `#79 Eval !` when
blocked. A checkmark means the role completed its workflow; the evaluation itself may still fail
and route the issue onward. GitHub remains the durable work ledger. Inspect routing and the retired
launcher's historical state with:

```bash
AGENT-TEAM/scripts/dispatcher-admin.sh status
AGENT-TEAM/scripts/dispatcher-admin.sh shadow
```

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

Oliver is a low-volume hobby project. Build, deploy, evaluation follow-up, culture review, and
product clarification are event-driven through the queue. Calendar schedules exist only for work
whose input is inherently a time window. Active Codex activity settings that belong to this team
are recorded in `automations.toml`; that registry is descriptive and must match the actual Codex
activity. All times America/Chicago.

| Role | Cadence |
|------|---------|
| Operations Manager | Event-driven by `dispatch:operations` |
| Build Manager | Event-driven by `dispatch:build` |
| Evaluator | Friday 14:30 production audit + event-driven acceptance/follow-up |
| Product Manager | Event-driven or manual discovery; proposals still require Jamie |
| Club Ethnographer | Event-driven for tone/memory/selection work |
| Manager | Every four weeks — team-health review + notes digest |

## North star

The goal is **not "more automation"** — it is better book club conversation: stronger picks,
better meeting readiness, more useful memory, and discussion prompts that reflect the club's real
reading history. Oliver should feel like a useful sixth member, grounded in the corpus.
