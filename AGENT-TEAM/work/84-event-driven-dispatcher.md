# Design — event-driven Oliver AGENT-TEAM dispatcher

**Issue:** [#84](https://github.com/jthingelstad/rwbookclub.com/issues/84)
**Decision:** approved directly by Jamie on 2026-07-31
**Status:** automatic app-visible thread creation is blocked on a supported non-interactive Codex
API; read-only selection plus on-demand visible project sessions is the current operating model

## Intended outcome

Replace independent role polling with one deterministic queue selector. GitHub remains the durable
ledger and approval boundary; Codex runs only when issue state names executable work. An idle scan
produces no model call, Codex session, issue comment, or run note. Real work uses normal persisted
Codex project sessions so Jamie can inspect and resume them.

The only calendar-driven agent activities are the Friday 14:30 performance evaluation and the
four-week Team Manager review. Build, Operations, Product, and Club Ethnographer are event-driven.

## State machine

One `dispatch:*` label names the next worker. `needs-eval`, `needs-culture`, and the existing
`needs-deploy` preserve pending downstream acceptance without overloading work-type labels.

```text
issue transition
      ↓
read-only shadow selector
      ↓
active Codex conversation claims the issue and creates a project thread
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
the owner's local IPC router was also rejected with `no-client-found`: app-owned automation and
project-thread operations are not exposed to an arbitrary launchd client.

Automatic launch is therefore disabled and the LaunchAgent is uninstalled. The selector remains
available in read-only shadow mode. A person or an active Codex app conversation uses its supported
project-thread tool to start the chosen role. That thread uses a compact live title such as
`#79 Eval · evidence` and ends in `#79 Eval ✓` or `#79 Eval !`.

The current operator runbook, including claim order, title vocabulary, final-state meanings, and
handoffs, lives in `AGENT-TEAM/README.md` → Visible role sessions. This design record explains why
the automatic launcher remains unavailable; it does not supersede that runbook.

Historical launcher JSONL can contain private evaluator evidence or tool output. It never enters
Git, GitHub, or a shared log; the owner-only state/log directory and files are mode 0700/0600.

## Failure boundaries

- Shared preflight runs before every claim; dirty, behind, diverged, or unexpectedly-ahead state
  prevents mutation.
- The role trusts issue state, not final prose. Success requires closure, an explicit human stop,
  or exactly one different next dispatch label.
- Failed or invalid on-demand runs release `wip` and leave the issue in an explicit recoverable
  state; they do not create invisible retry sessions.
- The active conversation starts at most one role. A new visible session is required for the next
  handoff, preventing an unbounded hidden role chain.
- Product proposals never cross Jamie's gate automatically.
- Build and Operations remain serialized. Roles never invoke each other directly.

## Deployment and rollback

Run the dispatcher only in `--shadow --all` mode. `dispatcher-admin.sh install` and `restart` fail
closed until Codex exposes a supported non-interactive way to create normal app-visible project
threads. `dispatcher-admin.sh uninstall` removes the retired LaunchAgent. GitHub retains every
issue and pending acceptance state.
