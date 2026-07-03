# Archived one-time scripts

These ran to completion and are kept for the record (their tests still guard them). Forward
capture is live in the agent itself, so none of these should need to run again.

| Script | What it did | Ran |
|---|---|---|
| `import_airtable.py` | One-time Airtable → club_* SQLite seed (reads the on-disk snapshot in `_airtable_cache/`) | 2026-06-26 |
| `mine_archive_memories.py` | Mined 10 years of mailing-list mail into member/club reflection memories | 2026-07-01 |
| `mine_archive_events.py` | Extracted historical club events from the mail archive into the timeline | 2026-06-30 |
| `mine_archive_book_cloud.py` | Seeded the Book Cloud (131 titles) from the mail archive | 2026-07-02 |
| `backfill_conversation_members.py` | Backfilled member_slug tags on pre-existing conversation rows | 2026-06-29 |

Operational scripts stay one level up: `webapp_local.py` (local webapp dev), `prune_backups.py`
(backup rotation), `dump_club_seed.py` (test-fixture regen — rerun after club_* schema changes),
`admin.sh`.
