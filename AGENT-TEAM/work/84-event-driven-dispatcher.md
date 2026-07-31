# Design — event-driven Oliver AGENT-TEAM dispatcher

**Issue:** [#84](https://github.com/jthingelstad/rwbookclub.com/issues/84)
**Decision:** approved directly by Jamie on 2026-07-31
**Status:** active through one dedicated app-visible dispatcher thread with a 15-minute heartbeat

## Intended outcome

Replace independent role polling with one deterministic queue selector. GitHub remains the durable
ledger and approval boundary; Codex runs only when issue state names executable work. An idle
heartbeat reuses the dispatcher thread and produces no child session, issue comment, run note, or
repository change. Real work uses normal persisted Codex project sessions so Jamie can inspect and
resume them.

The only calendar-driven agent activities are the Friday 14:30 performance evaluation and the
four-week Team Manager review. Build, Operations, Product, and Club Ethnographer are event-driven.

## State machine

One `dispatch:*` label names the next worker. `needs-eval`, `needs-culture`, and the existing
`needs-deploy` preserve pending downstream acceptance without overloading work-type labels.

```text
issue transition
      ↓
15-minute heartbeat in the dedicated dispatcher thread
      ↓
read-only shadow selector → preflight → wip claim → one project thread
      ↓
preflight → one role → authoritative GitHub/repository transition
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

The first implementation invoked roles with:

```text
codex exec --json --cd <repo> --model <role-model> ...
```

Those runs persist rollout files, but live acceptance on 2026-07-31 proved that `source=exec`
threads are absent from the Codex app's normal project-thread inventory. A second attempt through
the owner's local IPC router was rejected with `no-client-found`: app-owned project-thread tools
are not exposed to an arbitrary launchd client.

The working boundary is a normal app-visible dispatcher thread. One Codex heartbeat targets that
same thread every 15 minutes. The dispatcher runs the deterministic selector and preflight, refuses
to launch while any `wip` owns the shared checkout, claims one issue, and uses the supported Codex
project tool to create one normal role thread. The child receives the issue, route, claim, role
file, and authoritative GitHub completion protocol. It uses a compact title such as
`#79 Eval · evidence` and ends in `#79 Eval ✓` or `#79 Eval !`.

The heartbeat reuses the dispatcher thread, so it does not create a polling-thread archive every
15 minutes. Idle heartbeats make no durable transition. The runbook, title vocabulary, and failure
cleanup live in `AGENT-TEAM/dispatcher.md` and `AGENT-TEAM/README.md`.

## Failure boundaries

- Shared preflight runs before every claim; dirty, behind, diverged, or unexpectedly-ahead state
  prevents mutation.
- The role trusts issue state, not final prose. Success requires closure, an explicit human stop,
  or exactly one different next dispatch label.
- If project lookup or child creation fails, the dispatcher releases only its own `wip` and records
  the exact failure on the issue.
- Any existing `wip` makes the next heartbeat a no-op, keeping Build and Operations serialized on
  the shared checkout.
- One heartbeat starts at most one role; the next handoff waits for a later heartbeat.
- Product proposals never cross Jamie's gate automatically.
- Build and Operations remain serialized. Roles never invoke each other directly.

## Deployment and rollback

Create one normal `Oliver Dispatcher` project thread, then point the ACTIVE `oliver-dispatcher`
heartbeat at that thread with `FREQ=MINUTELY;INTERVAL=15`. The checked-in automation registry is the
descriptive source for that cadence. `dispatcher-admin.sh install` and `restart` remain disabled;
`uninstall` removes the retired LaunchAgent. GitHub retains every issue and pending acceptance
state, so rollback is pausing one heartbeat without losing the queue.
