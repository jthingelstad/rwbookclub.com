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
6. Run focused checks while iterating and
   `AGENT-TEAM/scripts/verify.sh` before commit. Commit and push only current-run work
   directly to `main`.
7. Complete the Run Oliver technical-acceptance phase in the same run: restart or publish
   only when the affected surface requires it, verify the intended revision and health,
   and do not manufacture member activity. Run Oliver owns this standard; the current
   objective owner executes it without changing owners or creating a handoff.
8. Never send Discord, email, DM, meeting, corpus, or site activity merely to validate
   a change.

## Ownership and acceptance

- Run Oliver owns the deployment, restart, and technical-health acceptance standard for
  every shipped change. Any objective that ships a change applies that phase before its
  run ends; Run Oliver audits the mechanism and the resulting health on its cadence.
- The originating objective owns semantic acceptance. Understand the Club proves the
  source model is faithful; Improve Oliver proves behavior or usefulness improved.
- A clean deploy never substitutes for natural semantic evidence.
- Safe, compatible schema evolution ships with its code and is rehearsed on a copy.
  Never point a new-code process or migration rehearsal at the live SQLite database.
- If technical acceptance cannot safely finish in the same run, the work has become a
  multi-run exception. Keep exactly one originating objective owner and record the commit,
  required technical action, technical predicate, semantic predicate, and next check in
  the issue and `Active watches`. A manually started Run Oliver pass may execute the
  technical action, but ownership remains with the originating outcome until acceptance.

## Issues are the exception ledger

Do not open an issue to authorize, claim, route, evaluate, or close same-run work. Retain
one only when work spans runs (including an incomplete deploy/acceptance phase), an external
dependency blocks it, Jamie must decide, or the arc needs a durable record. Give it exactly
one objective label.

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

Each active watch is one compact line containing: originating objective; change/commit;
technical state; semantic acceptance predicate; next natural event or date. Do not append
run history or duplicate an issue narrative.

## Reporting

End as `Healthy`, `Changed`, or `Needs decision`. Report the measured outcome and
remaining risk, not workflow ceremony. A monthly Improve Oliver pass may recommend one
specific contract correction when evidence shows duplicate work, collisions,
manufactured findings, or stalled acceptance; there is no separate Team Manager.
