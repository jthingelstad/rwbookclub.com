# Migration Blueprint: SQLite-authoritative club data (Airtable → SQLite → regenerated corpus + site)

> Blueprint only — no migration code yet. This is the design we build against over the
> coming weeks.

## Context — why, and the new shape of the work

The data layer is inconsistent: three stores — **Airtable** (the original, still in sync),
**corpus JSON** (treated as canonical), and **SQLite** (operational state) — with the ops
tables joined to club data by loose text columns (`meeting_key` = book slug, `member_slug`)
that have **no referential integrity**. The seam in one line: *"a meeting is keyed by its
book's slug — there is no meetings table; the meeting identity is borrowed from the book"*
(`agent/club/meeting_rules.py:64`).

The decision: **make SQLite authoritative with a proper relational schema** — integer
surrogate PKs and real foreign keys. Import the club record **straight from Airtable**
(which holds the proper autonumber IDs and typed fields, and has no slugs). **Throw the
corpus away** and regenerate a new, better one from the DB for Oliver; generate the website
data from the DB and **adapt the 11ty templates** to it. The corpus is disposable build
output, not a source — we are not preserving its shape or its quirks, and the one
Oliver-written test review (`patterns-in-nature--jamie.md`) is dropped.

**The one irreplaceable asset is the existing SQLite operational data** (attendance, roll
calls, reading statuses, contacts, email tracking, conversation history). It is neither in
Airtable nor the corpus. It is keyed today by `meeting_key` (slug) / `member_slug`; the
load-bearing step is remapping it onto the new `meeting_id` / `member_id` FKs without
orphaning a row.

```
 Airtable ──one-time import──▶ SQLite (authoritative: club record + ops state, integer PKs + FKs)
 (then retired)                    │  generate (build step)
                          ┌────────┴───────────┬──────────────────┐
                          ▼                    ▼                  ▼
                    new corpus           website data         cover images
                    (Oliver's brain,     (11ty; templates     (assets, via olKey)
                     designed fresh)      adapted to it)
```

## Target schema — new club-data tables in `agent/db.py` `_SCHEMA`

**Identity is an integer surrogate key everywhere — never a slug.** Slugs were invented by
`corpus/normalize.py`; Airtable never had them, and the new corpus will generate them only
as output filenames. Every PK is an integer `id`; every relationship is a real
`FOREIGN KEY … REFERENCES …(id)`. **PKs are Airtable's own autonumber surrogate keys**,
preserved at import: `Book ID`, `Meeting ID`, `Author ID`, `Review ID`. Only **members** and
**awards** have no Airtable autonumber → minted `AUTOINCREMENT`. New rows continue from
`MAX(id)+1`. The `email_tracking`/`mail_*` tables are the in-repo FK precedent;
`PRAGMA foreign_keys=ON` is already live (`db.py:353`).

Club record:
- `members(id PK AUTOINCREMENT, name, is_current, website, email NULL, mobile NULL)`
  — `email`/`mobile` recovered from Airtable (the corpus dropped them; Oliver needs them)
- `authors(id PK, name, bio NULL)`  — `id` == Airtable `Author ID`
- `books(id PK, title, subtitle, topic, fiction, publication_year, page_count, isbn13,
  ol_key, synopsis, subjects_json)`  — `id` == Airtable `Book ID`; `subjects` a JSON array
- `book_authors(book_id→books.id, author_id→authors.id, ordinal)`  — M:N
- `meetings(id PK, date, type_json, location, notes, placeholder)`  — `id` == Airtable `Meeting ID`
- `meeting_books(meeting_id→meetings.id, book_id→books.id, ordinal)`  — 0/1/2 books per meeting
- `picks(meeting_id→meetings.id, book_id→books.id, member_id→members.id)`  — host/picker per
  meeting (see Decision 1); generator aggregates to the corpus `book.picker[]`
- `reviews(id PK, airtable_id NULL, book_id→books.id, member_id→members.id, rating, dnf,
  discussion_quality, would_recommend, favorite_quote, body, created_at,
  UNIQUE(book_id,member_id))`  — `id` == Airtable `Review ID`; `airtable_id` keeps the `rec…`
  string for traceability
- `awards(id PK AUTOINCREMENT, name, year, award_category, notes)`
  + `award_books(award_id→awards.id, book_id→books.id)` + `award_voters(award_id→awards.id, member_id→members.id)`

Operational tables — **designed with real FKs from the start** (no slug columns):
`meeting_attendance`, `roll_calls`, `reading_statuses`, `member_contacts`, `email_tracking`,
and the member-scoped tables (`member_emails`, `member_identities`, `identity_claims`) now
carry `meeting_id`→`meetings.id` and/or `member_id`→`members.id` instead of
`meeting_key`/`member_slug`. Existing rows are migrated by mapping each old slug to its new
id (see Phase 3).

## Import: Airtable → SQLite (one-time)

Reuse the proven `corpus/fetch.py` reads + `corpus/migrate.py` field mapping, retargeted to
write **DB rows** instead of JSON. PKs come straight from the Airtable autonumbers; resolve
Airtable link fields (record-id links) → those autonumbers → our integer FKs (build a
`rec→id` map for members, which lack an autonumber). Recover member `email`/`mobile`/`photo`.
Validate row counts and relationship integrity against Airtable (verified live: Books 179,
Meetings 184, Authors 177, Members 12, Awards 1). `AIRTABLE_PAT` + `AIRTABLE_BASE_ID` are in
`.env`. The corpus is **not** consulted; Airtable is retired after this bootstrap.

## Generators: DB → outputs (functional gate, no parity with the old corpus)

The corpus is regenerated from scratch, so there is **no shape to reproduce** — the gate is
purely that the site renders correctly and Oliver answers as well or better.

- **New corpus (Oliver's brain).** Designed fresh for **maximum intelligence** — the
  richest, most retrievable representation we can produce, not a mirror of the old files.
  `corpus_read.py` + Oliver's tools are redesigned to the new shape (Decision 4). It can
  fold in mail/Discord/reading context already in SQLite (the payoff, Phase 6).
- **Website data.** Generated from the DB; the 11ty `_data/*.js` readers and templates are
  **adapted as needed** to the new data. Reuse `corpus/paths.py` slugify for page slugs.
- **Covers.** `corpus/images.py` regenerates from `olKey` idempotently — run after; not part
  of the JSON generator. Member photos remain manual.

## Oliver's write tools → DB transactions

`write_book` / `schedule_meeting` / `write_review` (and the future picking CRUD) collapse to:
**`db.connect()` transaction → upsert under FKs → regenerate the affected outputs →
`gitwrite.commit_paths(...)`**. The manual file-restore rollback blocks disappear (the
transaction's `conn.rollback()` covers it, `db.py:356`); the git commit/push survives as the
*publish* step. Delete/move/change-picker become trivial DELETE/UPDATE + regen — that is the
picking feature, now nearly free.

## Phased build (rebuilt off to the side, then cut over so live Oliver never breaks)

1. **Schema.** Define the new club-data tables **and** the redesigned FK'd ops tables in
   `db.py`, behind a `PRAGMA user_version` migration runner (Decision 2).
2. **Import club record.** Airtable → DB (above). Validate counts + links vs Airtable.
3. **Migrate ops data (the critical step).** Carry the existing SQLite ops rows onto the new
   FK schema: map every `meeting_key` slug → `meetings.id` and `member_slug` →
   `members.id`. Verify **zero orphans** — every attendance / roll-call / reading-status /
   email-tracking / contact / history row resolves to a real FK. Audit any slug that maps to
   nothing before dropping it.
4. **Generators + consumers.** Build DB → new corpus + website data; update `corpus_read`
   and Oliver's tools to the new corpus shape; adapt the 11ty templates. Delete the old
   `corpus/data/` tree once the site renders and the agent tests pass.
5. **Cutover.** Repoint Oliver's writers at the DB; `gitwrite` publishes the regenerated
   corpus + site. Airtable retired.
6. **Enrich (the payoff).** Fold the captured context already in SQLite — mailing-list
   threads, Discord history, reading activity — into Oliver's corpus so he's smarter than
   the old book/meeting-only fuel allowed. Iterative; no website impact.

## Decisions
1. **Pick modeling:** per-meeting host (`picks` table, recommended — correct grain,
   de-conflates book-vs-meeting). Generator still emits `book.picker[]`.
2. **Migration mechanism:** the flat `_SCHEMA` + additive `_migrate` (guarded `ALTER … ADD
   COLUMN`) can't do FK table rebuilds and has no versioning — add a minimal `PRAGMA
   user_version` runner.
3. **DB location vs CI:** generate + **commit** the corpus & website data; the site builds
   from the committed artifact via Pages-on-`main` (DB stays local on the bot host,
   gitignored). No DB in CI.
4. **Corpus/read-shape freedom — design for Oliver's best intelligence.** The new corpus and
   `corpus_read`'s return shapes are **redesigned from scratch** to maximize what Oliver can
   do — richest, most retrievable representation of the club's knowledge — not to match the
   old files. Agent-side churn (~54 call sites across tools/commands/context) is accepted as
   the cost of getting it right; the agent test suite is updated alongside.
5. **Hand-edit workflow:** post-cutover, edits go through the DB (a thin admin/CLI path); the
   corpus and site data are read-only generated output.

## Risks
- **The ops-data remap is the one that can lose data.** A bad slug→id map orphans attendance
  / roll-calls / reading history. Build an explicit verification (every ops FK resolves; zero
  orphans) and audit unmappable slugs before dropping them. This is the regression the whole
  effort turns on.
- Old corpus quirks (`a-distant-mirror.picker == ['dan','dan']`, books missing `subjects`)
  simply vanish — they were corpus-normalization artifacts, gone with the clean Airtable
  import.
- The large operational tables (mail archive, conversation history) stay local + gitignored;
  they're now also a source the corpus generator can draw on (Phase 6). Only the generated,
  curated outputs are committed.
- Rebuild happens off to the side and cuts over once verified, so the live Oliver (Discord +
  cadence emails) is never broken mid-flight.

## Verification
- **Import:** DB row counts == Airtable; spot-check book↔author, book↔meeting, picks,
  reviews relationships.
- **Ops remap:** zero orphaned FKs across `meeting_attendance` / `roll_calls` /
  `reading_statuses` / `member_contacts` / `email_tracking` / member tables.
- **Generators:** the new corpus passes a validate pass; `npm run build` renders the site
  correctly with adapted templates; the agent test suite is updated and green; spot-checked
  Oliver answers are as good or better.
- **Seam fix (built in, not bolted on):** moving/cancelling a meeting leaves attendance
  correctly attached via FK, not orphaned.
