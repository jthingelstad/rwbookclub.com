# DB backups

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
first for a gzipped one.
