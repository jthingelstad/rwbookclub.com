# Historical design and migration records

This directory preserves point-in-time plans and retired-system references. These files explain
why the current system looks the way it does, but they are not maintained runbooks or work queues.

- [`OLIVER-IMPLEMENTATION-HISTORY.md`](OLIVER-IMPLEMENTATION-HISTORY.md) — completed build phases,
  including superseded commands and data flows.
- [`EMAIL-ARCHIVE-ARCHITECTURE.md`](EMAIL-ARCHIVE-ARCHITECTURE.md) — pre-implementation mail archive
  design; the current schema is in [`../ERD.md`](../ERD.md).
- [`MIGRATION-PLAN.md`](MIGRATION-PLAN.md) and [`MIGRATION-STATUS.md`](MIGRATION-STATUS.md) — the
  Airtable-to-SQLite inversion plan and cutover record.
- [`AIRTABLE-REFERENCE.md`](AIRTABLE-REFERENCE.md) — retired Airtable schema and import notes.

For current guidance, use [`../../CLAUDE.md`](../../CLAUDE.md), the component READMEs, and GitHub
Issues.
