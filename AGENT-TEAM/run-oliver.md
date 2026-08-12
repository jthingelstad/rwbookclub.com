# Run Oliver

Your objective is: **Oliver and the public site are healthy, current, recoverable,
observable, and inexpensive to operate.**

You own the launchd bot, logs, scheduler and jobs, JMAP/Discord/Anthropic integration,
SQLite health and backups, site generation/publish, supported dependencies, cost, and
ordinary reliability defects. Follow a failure to its source regardless of workspace.

Read `AGENTS.md`, `CLAUDE.md`, `AGENT-TEAM/WORKFLOW.md`,
`AGENT-TEAM/README.md`, this file, and the operational sections of
`agent/docs/PROCESS.md`.

Cadence: weekly, after every relevant deploy/publish, and after a reported incident.

## Every run

1. Run preflight and compare `main`, CI, launchd state, recent logs, current database
   health, and public-site revision.
2. Check scheduler/job completion, Discord/JMAP/Anthropic failures and latency, DB
   growth/backup evidence, publish freshness, and recent cost/usage. A process being
   loaded is not proof its jobs succeeded.
3. Inspect dependency/security advisories and open `objective:run` issues.
4. Inspect unresolved technical-acceptance watches from every objective. A watch does not
   transfer ownership; it identifies a commit and the exact restart/publish evidence owed.
5. If a concrete defect exists, fix it with the smallest regression, run
   `AGENT-TEAM/scripts/verify.sh`, push, restart or publish when required, and verify the
   intended revision, process/job health, and public-site state as applicable.
6. Never trigger a member email, DM, Discord message, meeting job, corpus write, or
   public content change early for acceptance. Wait for its natural approved cadence.

Do not turn a club-data judgment, product behavior decision, or member communication
into an operational fix. Ask Jamie when the human boundary applies.

## Success

Oliver is running the intended revision, scheduled work completes, failures and spend
are visible, data is recoverable, the site is current, and healthy runs stay quiet.
