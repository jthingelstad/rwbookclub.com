# DB backups

One-off **pre-migration safety snapshots** of `agent/oliver.db`, named
`oliver-pre-<change>-<timestamp>.db`. They are **gitignored** (class B — never committed;
they're full copies of the live DB) and live here only as a local restore point.

Retention: keep the 2 most-recent snapshots uncompressed for a quick restore; gzip the rest.
Run after a migration:

```bash
python -m agent.script.prune_backups
```

Restore a gzipped backup with `gunzip <file>.db.gz`.
