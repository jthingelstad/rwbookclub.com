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

## Visible role sessions

Most Oliver team work runs as **one normal, app-visible Codex project thread per role**, not as a
background schedule. GitHub chooses the work and records the handoff; the Codex thread is the
visible, resumable execution record. This keeps the sidebar useful and prevents invisible role
runs from piling up.

### Start a role

One dedicated, app-visible `Oliver Dispatcher` thread receives a heartbeat every 15 minutes. It
runs the deterministic shadow selector, stops if any `wip` already owns the shared checkout,
preflights before claiming, and creates at most one normal local project thread for the selected
role. Idle heartbeats reuse that dispatcher thread and create no child thread, issue comment, run
note, or repository change.

The child role does exactly one focused run, updates its title at meaningful phases, and completes
the authoritative GitHub transition before its final response. It removes `wip` and its current
`dispatch:*` label, then closes the issue, stops at an explicit human state, or leaves exactly one
next `dispatch:*` label. It never invokes the next role itself; a later heartbeat handles that hop.

Manual recovery uses the same sequence in `AGENT-TEAM/dispatcher.md`. Never substitute `codex exec`
or a LaunchAgent: those sessions are not normal app-visible project threads.

### Live thread titles

Titles are compact status, not decoration. Set the base title immediately, change only the phase
suffix when the work materially advances, and keep the whole title at 24 characters or fewer.

| Role | Issue title base | Examples |
|------|------------------|----------|
| Build Manager | `#85 Build` | `#85 Build · code`, `#85 Build · tests` |
| Operations Manager | `#85 Ops` | `#85 Ops · deploy`, `#85 Ops · verify` |
| Evaluator | `#85 Eval` | `#85 Eval · evidence`, `#85 Eval · golden` |
| Club Ethnographer | `#85 Culture` | `#85 Culture · sources`, `#85 Culture · finding` |
| Product Manager | `#85 Product` | `#85 Product · signal`, `#85 Product · brief` |
| Manager | `#85 Team` | `#85 Team · queue`, `#85 Team · digest` |

For a calendar run with no issue, use `Eval W31` or `Team W31`. For manual discovery before an
issue exists, use `Product Scan` or `Culture Scan`; rename it to the issue form once work is
claimed.

Finish with `✓` only after the role has made a valid GitHub/repository transition, or `!` when it
is blocked or cannot complete safely: `#85 Eval ✓`, `#85 Build !`. A checkmark means **the role run
completed correctly**. It does not mean an evaluation passed or that the issue is necessarily
closed; for example, an Evaluator can correctly finish a failing evaluation and route the issue to
Build.

### GitHub handoffs

GitHub issue state, not a role's calendar or final prose, drives executable handoffs.

| Handoff label | Worker |
|---------------|--------|
| `dispatch:build` | Build Manager |
| `dispatch:operations` | Operations Manager |
| `dispatch:evaluator` | Evaluator |
| `dispatch:culture` | Club Ethnographer |
| `dispatch:product` | Product Manager |
| `dispatch:manager` | Manager |

Exactly one handoff label is active at a time. `needs-eval` and `needs-culture` remember downstream
acceptance still owed while `needs-deploy` retains its existing operational meaning. The role owns
the dispatcher's `wip` claim, removes its current handoff before finishing, and either
closes the issue, puts it in an explicit human stop state (`proposal`, `blocked`, `needs-design`),
or adds exactly one next handoff. Product proposals still wait for Jamie; defects and approved work
may flow autonomously.

The retired launchd watcher is intentionally uninstalled. The app-owned heartbeat runs inside a
normal Codex thread, where the supported project-thread tools are available. Inspect configuration
and read-only routing with:

```bash
AGENT-TEAM/scripts/dispatcher-admin.sh check
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
activity. Do not create recurring activities for the event-driven roles and do not reinstall the
retired launchd dispatcher. The single `Oliver Dispatcher` heartbeat is the event-driven trigger;
every real role still runs in a normal visible project thread and follows the title protocol above.
All times America/Chicago.

| Role | Cadence |
|------|---------|
| Operations Manager | Event-driven by `dispatch:operations` |
| Build Manager | Event-driven by `dispatch:build` |
| Evaluator | Friday 14:30 production audit + event-driven acceptance/follow-up |
| Product Manager | Event-driven or manual discovery; proposals still require Jamie |
| Club Ethnographer | Event-driven for tone/memory/selection work |
| Manager | Every four weeks — team-health review + notes digest |

For Oliver, the four-week Manager cadence in `automations.toml` and this table overrides the generic
weekly cadence in the byte-identical shared `manager.md` role. Keep the shared role file unchanged;
the project README owns project-specific cadence.

## North star

The goal is **not "more automation"** — it is better book club conversation: stronger picks,
better meeting readiness, more useful memory, and discussion prompts that reflect the club's real
reading history. Oliver should feel like a useful sixth member, grounded in the corpus.
