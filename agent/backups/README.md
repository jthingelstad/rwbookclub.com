# DB backups

This directory contains private member and mailbox state. Oliver's admin preflight keeps the
directory owner-only (`0700`) and backup files owner-readable/writable only (`0600`).

Full copies of `agent/oliver.db`. All are **gitignored** (class B — never committed; they hold the
live DB, PII included) and live here only as local restore points. Two kinds:

**Restart snapshots** — `oliver-restart-<timestamp>.db`. Taken automatically by
`agent/script/admin.sh` on every `restart`/`upgrade` (while Oliver is stopped), via SQLite's online
`.backup` (WAL-safe), immediately before the DB is `VACUUM`ed. The script keeps the
`KEEP_RESTART_BACKUPS` (10) most-recent and prunes older ones. Run a snapshot by hand with
`agent/script/admin.sh backup`.

**Migration snapshots** — one-off `oliver-pre-<change>-<timestamp>.db`, taken before a risky
migration. Retention: keep the 2 most-recent uncompressed for a quick restore, gzip the rest —
`python -m agent.script.prune_backups`.

Restore: copy a `.db` back over `agent/oliver.db` while Oliver is stopped; `gunzip <file>.db.gz`
first for a gzipped one. The next `agent.bot` start explicitly runs `agent.database.initialize()`
and upgrades the restored file through the ordered `schema_migrations` ledger before connecting to
Discord.

## Migration support floor

Every retained restart or migration snapshot is supported, including pre-ledger databases. Do not
delete or squash a migration while any retained backup may predate it. A future baseline/squash is
safe only after the oldest supported local and off-machine backup was created at or beyond that
baseline, and after a restore rehearsal has verified the replacement bootstrap.
