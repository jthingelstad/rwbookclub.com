# AGENT-TEAM operating model

Oliver is maintained by three objective owners. An owner is accountable for an
outcome, not a job type or directory. It follows evidence through diagnosis, code,
tests, deployment, and natural acceptance instead of handing each step to another
role.

Read `AGENTS.md` -> `CLAUDE.md` -> this file -> `AGENT-TEAM/README.md` -> the
selected objective file before acting. Read `agent/docs/SOUL.md`, `PURPOSE.md`, and
`PROCESS.md` whenever behavior or member context is in scope.

## Operating loop

1. Run `AGENT-TEAM/scripts/preflight.sh`. A dirty, behind, diverged, detached, or
   unexpectedly ahead checkout makes the run read-only. Never publish a pre-existing
   commit.
2. Measure current state from exact logs, launchd state, SQLite, JMAP/Discord evidence,
   the public site, CI, or the active issue as appropriate.
3. Decide whether a real objective gap exists. Healthy is a complete result.
4. If the gap is safe and authorized, fix it at the source in the same run. Add the
   business-rule regression; do not substitute a warning, prompt rule, or ticket chain.
5. Recheck branch, upstream, worktree, and other active work immediately before the
   first edit and before push. Stop if the state changed.
6. Run focused checks while iterating and the repository's complete local gates before
   commit. Commit and push only current-run work directly to `main`.
7. Run Oliver owns restart/publish and technical-health acceptance. The originating
   objective retains semantic acceptance from natural club evidence.
8. Never send Discord, email, DM, meeting, corpus, or site activity merely to validate
   a change.

## Ownership and acceptance

- Run Oliver owns deployment, restart, and technical-health acceptance for every
  shipped change.
- The originating objective owns semantic acceptance. Understand the Club proves the
  source model is faithful; Improve Oliver proves behavior or usefulness improved.
- A clean deploy never substitutes for natural semantic evidence.
- Safe, compatible schema evolution ships with its code and is rehearsed on a copy.
  Never point a new-code process or migration rehearsal at the live SQLite database.

## Issues are the exception ledger

Do not open an issue to authorize, claim, route, deploy, evaluate, or close same-run
work. Retain one only when work spans runs, an external dependency blocks it, Jamie
must decide, or the arc needs a durable record. Give it exactly one objective label.

There are no dispatch labels, handoff labels, `wip` claims, commit lanes, or manager
digests. Descriptive labels do not transfer ownership. An objective keeps the issue
until its acceptance condition is met.

## Human boundary

Jamie decides new member-facing behavior, communication cadence, schedule/order
changes, non-review corpus writes, privacy-affecting collection, and other significant
product direction. Ask one concrete yes/no question with evidence and the smallest
useful version.

Ordinary defects, reliability, observability, documentation, eval tooling, generated
corpus correctness, and narrow behavior-quality fixes are autonomous when they
preserve that boundary.

## Automation memory

Automation memory contains only `Current state`, `Active watches`, and one
replace-in-place `Latest run`. Remove resolved watches. Git, issues, logs, SQLite, CI,
and evaluation artifacts hold history.

## Reporting

End as `Healthy`, `Changed`, or `Needs decision`. Report the measured outcome and
remaining risk, not workflow ceremony. A monthly Improve Oliver pass may recommend one
specific contract correction when evidence shows duplicate work, collisions,
manufactured findings, or stalled acceptance; there is no separate Team Manager.
