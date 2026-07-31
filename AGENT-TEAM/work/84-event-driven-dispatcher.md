# Design — event-driven Oliver AGENT-TEAM dispatcher

**Issue:** [#84](https://github.com/jthingelstad/rwbookclub.com/issues/84)
**Decision:** approved directly by Jamie on 2026-07-31

## Outcome

Replace independent role polling with one deterministic queue watcher. GitHub remains the durable
ledger and approval boundary; Codex runs only when issue state names executable work. Idle polls
produce no model call, Codex session, issue comment, or run note. Real work uses normal
project-visible Codex threads so Jamie can inspect and resume them.

The only calendar-driven agent activities are the Friday 14:30 performance evaluation and the
four-week Team Manager review. Build, Operations, Product, and Club Ethnographer are event-driven.

## State machine

One `dispatch:*` label names the next worker. `needs-eval`, `needs-culture`, and the existing
`needs-deploy` preserve pending downstream acceptance without overloading work-type labels.

```text
issue transition
      ↓
launchd poll (deterministic, silent when idle)
      ↓
preflight → wip claim → one project-visible Codex role thread
      ↓
authoritative GitHub/repository re-read
      ↓
closed | explicit human stop | exactly one next dispatch label
```

Typical behavior finding:

```text
weekly Evaluator → dispatch:build + needs-eval
Build → dispatch:operations + needs-deploy + needs-eval
Operations → dispatch:evaluator + needs-eval
Evaluator pass → close
```

New direction stops at `proposal`. `blocked`, `needs-design`, and `wip` are non-routable. A legacy
or human-filed issue may be seeded only through narrow deterministic rules (`needs-deploy`, pending
review, defect, approved+ready); explicit dispatch always wins.

## Execution and visibility

`AGENT-TEAM/scripts/dispatcher.py` is invoked by
`com.rwbookclub.agent-team-dispatcher` every two minutes. It holds an advisory file lock for the
entire role run, so the shared checkout has one mutating owner. For a real handoff it uses a
short-lived, paused Codex activity only as an app bridge. That bridge calls the supported project
thread creation capability, gives the child the exact role prompt/model/effort, titles it compactly,
and archives itself. The transient activity is then deleted; it has no calendar cadence.

The durable artifact is therefore a normal thread in the `rwbookclub.com` Codex project, not a
hidden `codex exec` rollout and not a standing role schedule. Titles start compactly (`#81 Eval`),
may expose a useful current phase (`#81 Eval · tests`), and end with `✓` or `!`. GitHub receives a
claim comment and an authoritative transition result. `dispatcher-admin.sh status` shows the
active and ten most recent roles with their app-visible thread IDs.

Codex owns the full private transcript and rollout. The dispatcher keeps only owner-only state,
events, and small thread/rollout pointers; none enters Git, GitHub, or a shared log. The local
state directory and files are mode 0700/0600 and old pointers are retained for 30 days.

## Failure boundaries

- Shared preflight runs before every claim; dirty, behind, diverged, or unexpectedly-ahead state
  prevents mutation.
- The dispatcher trusts issue state, not final prose. Success requires closure, an explicit human
  stop, or exactly one different next dispatch label.
- Failed/invalid runs release dispatcher-owned `wip` and retry with 5-minute, 30-minute, and
  2-hour backoff. Three failures add `blocked`.
- Four role hops within 90 minutes stop the chain and add `blocked`.
- Product proposals never cross Jamie's gate automatically.
- Build and Operations remain serialized. Roles never invoke each other directly.

## Deployment and rollback

Run the dispatcher in `--shadow --all` first. After label and routing verification, install the
checked-in plist with `AGENT-TEAM/scripts/dispatcher-admin.sh install`. Rollback is bounded: stop
that one LaunchAgent and reactivate the paused sparse Codex schedules. GitHub retains every issue,
claim, transition, and pending acceptance state.
