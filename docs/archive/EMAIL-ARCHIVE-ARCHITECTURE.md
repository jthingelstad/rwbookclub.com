# Oliver Email Archive and Unified Conversation Architecture

> Historical design record. The implemented schema is documented in `docs/ERD.md`; current
> mail behavior and privacy boundaries are documented in `agent/README.md` and `CLAUDE.md`.
> Names, phases, and open questions below reflect the pre-implementation design and are not an
> active work queue.

> **STATUS: implemented (historical design note).** The mail archive is live (~2,448 `mail_messages`,
> all member-linked) and reachable via the `search_mail_archive` / `get_mail_thread` tools. Since
> this was written, the ops tables (`meeting_attendance`, `reading_statuses`, etc.) were moved from
> `(meeting_key, member_slug)` text keys to integer `meeting_id` / `member_id` FKs — read the keying
> details below as historical.
>
> **UPDATE (2026-06-27): member identity consolidated.** The parallel identity surfaces this note
> describes — the `mail_participants` / `mail_participant_addresses` person store and the
> `identity_claims` review queue — were **removed**. There is now ONE identity model: `club_members`
> (the person) + `member_identities` (their handles: `surface ∈ {discord, email, sms}`). The archive
> attributes each message via `mail_messages.member_id` (FK → `club_members`, resolved through
> `member_identities` + a corpus name fallback) — the only column the read paths use. Unresolved
> senders keep `member_id` NULL (no claim is filed); link the member later (web app member editor) and
> `mail_archive.reattribute_archive()` updates their history. Treat every `mail_participants` /
> `mail_participant_addresses` / `identity_claims` mention below as historical.

This document describes the target architecture for giving Oliver durable access to the R/W Book Club mailing-list archive while keeping Discord and email tied to one member identity and one operational meeting state.

It was originally a design note for review before implementation.

## Goals

- Import the historical mailing-list archive, expected to be about 2,500 messages.
- Continue capturing future mailing-list messages automatically through Oliver's subscribed email address.
- Treat Discord and email as two surfaces for the same people, not as separate memory silos.
- Let Oliver retrieve historical email context when useful without stuffing the whole archive into prompts or turning every email into a memory.
- Keep meeting planning state unified: an attendance or reading-progress answer in Discord must affect what Oliver does by email, and the reverse must also be true.

## Non-goals

- Do not make Airtable a live dependency. The email archive belongs in Oliver's private SQLite state, not the public Git corpus.
- Do not publish private mailing-list content through the website.
- Do not treat all historical email as trusted durable memory. Raw email is evidence; memory is a curated/distilled layer.
- Do not widen Oliver's email-reply policy. Mailing-list reply decisions remain conservative and separate from archive capture.

## Current State

Oliver already has the right identity and meeting-state foundation:

- `member_identities` maps Discord user IDs to `member_slug`.
- `member_emails` maps email addresses to `member_slug`.
- `meeting_attendance` is keyed by `(meeting_key, member_slug)`.
- `reading_statuses` is keyed by `(meeting_key, member_slug)`.
- `member_contacts` logs contact attempts and replies by `member_slug`.
- `oliver.answer()` resolves the active speaker to a `member_slug` from either Discord user ID or `email:<address>`.
- Member-scoped memories are loaded by `member_slug`, so a recognized email sender and the same person in Discord already share Oliver memories.
- Discord-visible conversation turns are stored in `conversations`.
- Email processing status is stored in `inbound_emails`, but that table is a dedupe/status ledger only. It does not store the full email body and should not become the mail archive.

Current gap:

- There is no full-fidelity email message store.
- There is no email full-text search or thread retrieval tool.
- Historical mailing-list messages are not connected to Oliver's retrieval path.
- `conversations` records channel, role, speaker display name, and content, but not durable per-row source identity/provenance. Future cross-surface history should add a bridge from conversation rows to source message IDs and `member_slug`.
- `search_discussion` is currently Discord-oriented and assumes numeric channel IDs when labeling results, so it needs cleanup before email pseudo-channel IDs are searched through the same path.

## Archive Assessment: `tmp/topics.mbox`

I reviewed the supplied Google Groups mbox on 2026-06-25.

Fit summary:

- The archive is structurally suitable for the proposed SQLite import.
- The implementation should use Gmail thread IDs as the primary historical thread key.
- The implementation needs a sender alias/address layer, not just `member_slug`, because the archive contains aliases and older sender addresses.
- Recipient-only addresses do not need identity treatment for this import. Keep them in raw `to_json`/`cc_json`, but do not create participant/member records from recipients alone.
- Attachment content should not be imported in v1. Attachments are uncommon and add a disproportionate amount of complexity.
- The implementation must clean Google Groups footers and quoted history before indexing.

Observed archive shape:

- File: `tmp/topics.mbox`
- Size: 193,004,855 bytes
- Messages: 2,441
- Date range: 2016-08-12 to 2026-06-09
- Gmail threads: 538 unique `X-GM-THRID` values
- Every message has a unique `Message-ID`
- Every message has `X-GM-THRID`
- `References` appears on 1,932 messages
- `In-Reply-To` appears on 1,915 messages
- 8,427 of 8,519 reference tokens point to messages present in the archive
- `List-Id`, `Mailing-list`, and `List-Post` are absent
- `X-BeenThere` appears on 2,439 messages and should be the main list-membership signal, backed by `To`/`Cc`

Sender identity observations:

- There are only 9 sender addresses.
- Existing `member_emails` resolves 2,392 of 2,441 messages to current member slugs.
- The remaining 49 sender messages are alternate addresses:
  - `terveen@cs.umn.edu` -> Loren, 35 messages
  - `erik@erik.jordan.name` -> Erik, 11 messages
  - `jthingelstad@gmail.com` -> Jamie, 2 Google Sheets messages
  - `snowfall@acm.org` -> Tom, 1 message
- Header recipients also include auxiliary addresses that should be preserved in message headers but not treated as members:
  - `hectorguatemala@gmail.com`, 10 `To`/`Cc` appearances
  - `erik.jordan@gmail.com`, 3 `To`/`Cc` appearances
  - `nswenson@airt.net`, 3 `To`/`Cc` appearances
  - `rwbookclub@lists.thingelstad.com` and `rwbookclub@listbox.com`, one old list address each

Content observations:

- 2,344 messages have `text/plain`
- 2,234 messages have `text/html`
- 166 messages are HTML-only and need HTML-to-text conversion
- 135 messages have attachments; 222 attachment parts total, about 101 MB
- Attachment parts are mostly images: 139 JPEG, 61 PNG, plus 2 GIF, 1 WebP, and 1 TIFF
- Non-image attachment parts are limited: 8 PDFs, 8 PowerPoint decks, 1 spreadsheet, and 1 Word document
- Those non-image attachments collapse to 16 unique file hashes
- The PowerPoint decks and Word document contain high-value text for book-club context, especially picker meeting candidates and book notes
- The PDFs include a mix of useful club artifacts, such as book-pick decks and survey results, and less central side-topic documents
- The spreadsheet appears to be side-topic data rather than core book-club memory
- Given the low count and mixed value, attachment text extraction should be deferred until there is a concrete retrieval miss that justifies it
- Google Groups footer text appears in about 1,787 messages
- Raw plain bodies are much larger than the real current message because quoted history is included. Median plain body length is about 2,103 characters, while a rough footer/quote-cleaned body median is about 221 characters.

Data usefulness:

- The archive contains meeting logistics, book picks, polls, voting, reading status, attendance signals, restaurant/location decisions, Zoom links, Trello/Airtable/Sheets coordination, book recommendations, and side-channel links.
- This is excellent retrieval context for Oliver, but it should not be replayed into current meeting state by default.
- The mbox begins in 2016 because that is when the Google Groups list history begins. The 2003-2016 period was 1-1 email rather than mailing-list email and is outside this import unless a separate archive appears later.

Import recommendation:

- Import all 2,441 message records.
- Do not exclude side-topic messages: they are part of the book club's shared list history and were sent by members to the list.
- Do not import attachment blobs or extracted attachment text in v1.
- Do not index Google Groups footers or quoted history.
- Do not promote historical emails into current meeting state automatically.

## Core Invariants

1. `member_slug` is the canonical person key.

   Discord IDs, email addresses, historical sender names, and future aliases are identity claims. They resolve to a member only when there is an explicit trusted mapping.

2. Operational meeting state is surface-independent.

   Attendance, reading progress, contact history, and meeting readiness must be keyed by `member_slug`, not Discord ID, email address, or channel.

3. Raw archive is not memory.

   Raw email and Discord history are retrieval evidence. The `memories` table remains Oliver's distilled long-term notes. A memory can cite an email message or thread as its source, but import should not automatically convert every email into memory.

4. Historical import and live ingest write the same archive tables.

   The one-time import and future subscribed-list messages must produce the same normalized `mail_messages`, `mail_threads`, and search indexes.

5. Capture is broader than reply.

   Oliver should archive allowed mailing-list messages even when it correctly decides not to reply. Reply policy controls outbound behavior, not whether the message becomes available as history.

6. Archive senders can be broader than current operational members.

   The archive may include former members and sender aliases. Per the review assumption for this archive, senders who appear as club participants should be treated as club history, not as spam or outside senders. Recipient-only addresses should not create member or participant records.

## Proposed Data Model

Keep existing tables and add a mail archive layer.

### `mail_threads`

One row per mailing-list thread or direct email thread.

Columns:

- `thread_id TEXT PRIMARY KEY`: stable internal thread key. For this mbox, use `x-gm-thrid:<X-GM-THRID>`.
- `list_id TEXT`: mailing-list identifier. For this mbox, derive from `X-BeenThere` or the `rwbookclub@googlegroups.com` recipient because `List-Id` is absent.
- `subject_normalized TEXT`: subject with common `Re:`/`Fwd:` noise removed.
- `first_sent_at TEXT`
- `last_sent_at TEXT`
- `message_count INTEGER NOT NULL DEFAULT 0`
- `participants_json TEXT`: normalized sender/member/email summary.
- `summary TEXT`: optional rolling thread summary.
- `summary_model TEXT`
- `summary_updated_at TEXT`
- `created_at TEXT NOT NULL DEFAULT (datetime('now'))`
- `updated_at TEXT NOT NULL DEFAULT (datetime('now'))`

### `mail_messages`

One row per email message.

Columns:

- `message_id TEXT PRIMARY KEY`: RFC `Message-ID` when available, otherwise a stable hash from source, sender, timestamp, subject, and body.
- `thread_id TEXT NOT NULL`
- `parent_message_id TEXT`: derived from `In-Reply-To` or `References` when available.
- `source TEXT NOT NULL`: `historical_import` or `live_jmap`.
- `source_ref TEXT`: export path, mailbox ID, or JMAP email ID.
- `list_id TEXT`
- `sender_participant_id INTEGER`
- `from_email TEXT`
- `from_name TEXT`
- `member_slug TEXT`: resolved sender, nullable for unknown or non-member senders.
- `to_json TEXT`
- `cc_json TEXT`
- `subject TEXT`
- `sent_at TEXT`
- `received_at TEXT`
- `body_text TEXT`: plain text body as received or converted from HTML.
- `body_clean TEXT`: quoted history, list footers, and tracking boilerplate removed when possible.
- `body_html TEXT`: optional, only if we decide the storage value is worth the size/privacy cost.
- `attachments_json TEXT`: optional compact attachment manifest with filename, content type, size, disposition, and content ID. Do not store blobs or extracted attachment text in v1.
- `headers_json TEXT`
- `imported_at TEXT NOT NULL DEFAULT (datetime('now'))`
- `processed_inbound_email_id TEXT`: links to `inbound_emails.email_id` for live mail, nullable for historical import.

Indexes:

- `idx_mail_messages_thread_sent` on `(thread_id, sent_at)`
- `idx_mail_messages_member_sent` on `(member_slug, sent_at)`
- `idx_mail_messages_from_email` on `(from_email)`
- `idx_mail_messages_sent_at` on `(sent_at)`

### `mail_message_fts`

An SQLite FTS5 virtual table over:

- `subject`
- `from_name`
- `from_email`
- `body_clean`

This keeps archive search fast and avoids overloading the existing `conversations` LIKE search. Index `body_clean`, not the raw quoted body, so search results do not get dominated by repeated Google Groups footers and quoted replies.

### `mail_participants`

One row per person or list identity observed in the archive.

Columns:

- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `display_name TEXT`
- `member_slug TEXT`: nullable link to the canonical corpus/Oliver member.
- `participant_type TEXT NOT NULL DEFAULT 'person'`: `person`, `list`, `system`, or `unknown`
- `membership_status TEXT`: `current`, `former`, `historical`, `guest`, or `unknown`
- `notes TEXT`
- `created_at TEXT NOT NULL DEFAULT (datetime('now'))`
- `updated_at TEXT NOT NULL DEFAULT (datetime('now'))`

For this archive, unresolved human participants should default to `historical` rather than being treated as external spam.

### `mail_participant_addresses`

One row per email address observed in the archive.

Columns:

- `email TEXT PRIMARY KEY`
- `participant_id INTEGER NOT NULL`
- `member_slug TEXT`
- `display_name TEXT`
- `source TEXT NOT NULL`: `member_emails`, `historical_import`, `manual`, or `live_jmap`
- `confidence REAL NOT NULL DEFAULT 1.0`
- `created_at TEXT NOT NULL DEFAULT (datetime('now'))`
- `updated_at TEXT NOT NULL DEFAULT (datetime('now'))`

Known aliases from this mbox should be accepted before or during import:

- `terveen@cs.umn.edu` -> `loren`
- `erik@erik.jordan.name` -> `erik`
- `jthingelstad@gmail.com` -> `jamie`
- `snowfall@acm.org` -> `tom`

Potential sender aliases to review if they ever appear as senders in another archive or live mail:

- `erik.jordan@gmail.com` -> likely `erik`
- `nswenson@airt.net` -> likely `nick`

### `identity_claims`

An optional review queue for historical sender addresses that are not confidently mapped.

Columns:

- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `surface TEXT NOT NULL`: `email` or `discord`
- `identifier TEXT NOT NULL`: email address, Discord ID, or other handle.
- `display_name TEXT`
- `candidate_member_slug TEXT`
- `confidence REAL`
- `status TEXT NOT NULL DEFAULT 'pending'`: `pending`, `accepted`, `rejected`, `ignored`
- `evidence_json TEXT`
- `created_at TEXT NOT NULL DEFAULT (datetime('now'))`
- `resolved_at TEXT`
- `resolved_by TEXT`

Accepted email claims should write to `member_emails`; accepted Discord claims should write to `member_identities`. Oliver should not auto-link uncertain historical aliases.

### `conversation_links`

Optional bridge from the existing `conversations` table to source records.

Columns:

- `conversation_id INTEGER NOT NULL`
- `surface TEXT NOT NULL`: `discord`, `email`, or `mail_archive`
- `source_message_id TEXT NOT NULL`
- `thread_id TEXT`
- `member_slug TEXT`

This avoids a disruptive migration of the existing `conversations` table while still allowing audit and provenance.

Future Discord and email conversation logging should create this link whenever a source message ID and resolved `member_slug` are known. Existing historical `conversations` rows can remain as-is and be resolved best-effort by channel/speaker only.

## Historical Import Flow

The importer should be idempotent and safe to run repeatedly.

1. Parse the export format into normalized message objects.
2. Use RFC `Message-ID` as `message_id`. This mbox has one on every message; stable hash fallback remains for future imports.
3. Use `X-GM-THRID` as the historical `thread_id`. Subject fallback remains for future imports that do not have Gmail thread IDs.
4. Derive `list_id` from `X-BeenThere` and `To`/`Cc` because this mbox does not have `List-Id`.
5. Normalize sender, recipient, and reply-to addresses.
6. Create or update `mail_participants` and `mail_participant_addresses`.
7. Resolve `member_slug` from `member_emails` and accepted archive aliases.
8. Add unresolved senders to `identity_claims` or `mail_participants` as historical participants rather than rejecting them.
9. Extract plain text when present; convert HTML-only messages to text.
10. Clean message text into `body_clean` by removing Google Groups footers, quoted history, and signature/tracking boilerplate where practical.
11. Optionally record a compact attachment manifest in `attachments_json`; do not store attachment blobs or extracted attachment text.
12. Upsert `mail_threads`, `mail_messages`, and `mail_message_fts` in transactions.
13. Produce a dry-run report before writing:
   - total messages
   - thread count
   - date range
   - sender count
   - recognized member messages
   - unresolved sender addresses
   - duplicate/skipped messages
   - messages without usable dates or IDs
   - HTML-only message count
   - attachment count by content type

The first implementation should include a dry-run mode and a write mode. We should review the dry-run identity report before accepting any new historical email aliases. For this mbox, the likely alias set is small enough to seed manually before the full write.

## Live Email Ingest Flow

Future mailing-list mail should enter the same archive path before Oliver decides whether to reply.

Current high-level flow:

1. Oliver sees an unread message in `Inbox/Oliver`.
2. `email_policy.inbound_decision()` applies hard safety gates.
3. `inbound_emails` claims the message for dedupe/status.
4. Oliver may reply or ignore depending on policy and model decision.

Target flow:

1. Oliver sees an unread message in `Inbox/Oliver`.
2. `email_policy.inbound_decision()` applies hard safety gates.
3. `inbound_emails` claims the message for dedupe/status.
4. If the message is from an allowed member or the book club mailing list, archive it into `mail_messages`, `mail_threads`, `mail_participants`, `mail_participant_addresses`, and FTS.
5. Record member contact and explicit meeting state as today.
6. If it is mailing-list mail, run the conservative one-turn answer/no-reply decision.
7. If Oliver ignores it, log the ignored decision as today. The archived message remains searchable.
8. If Oliver replies, link the outbound reply back to the source message/thread.

Messages blocked by hard safety policy, such as no-reply senders, bounces, invites, and unknown senders, should remain outside the archive unless we explicitly add a quarantine table. They can still be logged as ignored operational events.

## Retrieval and Tools

Oliver should not load the whole archive into normal context. It should retrieve narrow slices.

Add tools:

### `search_mail_archive`

Search historical and live email.

Inputs:

- `query`
- `member` optional member slug or name
- `year_from` optional
- `year_to` optional
- `limit` default 8, max 20

Output should include compact snippets:

- message ID
- thread ID
- subject
- sender display and participant ID
- resolved member, if any
- sent date
- snippet

### `get_mail_thread`

Fetch a compact thread transcript.

Inputs:

- `thread_id`
- `limit` default 20, max 50

Output:

- thread metadata
- summary when available
- chronological messages with sender, date, and cleaned body excerpt

### `member_conversation_history`

Show a cross-surface recent/history view for one member.

Inputs:

- `member`
- `participant_id` optional, for historical participants without a `member_slug`
- `surface` optional: `discord`, `email`, or `all`
- `limit`

Output should combine:

- recent Discord `conversations` rows where resolvable
- recent `mail_messages`
- durable `memories`
- meeting attendance and reading status

This is a diagnostic/admin tool more than a casual answer tool.

When `member` is unavailable because the person is not in the corpus, this tool should still support participant/address lookup over the archive. It should not pretend that participant has current operational meeting state.

### Search Strategy

Keep `search_discussion` Discord-focused initially, but fix it so non-numeric channel IDs cannot crash result labeling. Then add email-specific archive tools.

After the email archive is stable, add a unified facade such as `search_club_conversation_history` that searches both Discord and email and returns surface-tagged results.

## Memory Interaction

The `memories` table should continue to hold small durable notes, not raw conversation history.

Expected flow:

1. Oliver searches Discord or mail history when a question calls for prior conversation evidence.
2. Oliver answers from retrieved snippets, with enough specificity to be useful.
3. If a retrieved fact is durable and likely to matter later, Oliver may save a memory with:
   - `scope = 'member'` or `scope = 'club'`
   - `subject = member_slug` for member memory
   - `source = 'mail'` or `source = 'discord'`
   - `source_message_id = mail_messages.message_id` or Discord message ID when known

This keeps memory compact and sourceable while preserving the full archive for retrieval.

## Identity Model

The identity resolver should be explicit and auditable.

Canonical person:

- `member_slug`

Archive participant:

- `mail_participants.id`

Trusted identifiers:

- Discord user IDs in `member_identities`
- Email addresses in `member_emails`
- Accepted historical email aliases in `mail_participant_addresses`, optionally mirrored into `member_emails` when they belong to a known member

Untrusted or pending identifiers:

- Historical sender aliases in `identity_claims`
- Recipient-only archive addresses
- Sender display names from email headers
- Mentions inside message bodies

Resolution rules:

1. Exact trusted email address wins.
2. Exact trusted Discord ID wins.
3. Accepted `mail_participant_addresses` can identify historical archive participants even when there is no current `member_slug`.
4. Display-name fallback may help current conversational context, but it must not create a durable link by itself.
5. Historical import may propose identity claims, but humans should accept/reject uncertain mappings.
6. A member can have multiple email addresses and one or more Discord IDs over time.
7. A historical participant can exist in the archive without current meeting obligations.

Operational guarantee:

If Jamie answers roll call in Discord and later Oliver evaluates whether to send Jamie a roll-call email, Oliver checks `meeting_attendance(meeting_key, jamie)` and should not send the email. If Jamie answers by email first, Discord tools see the same row.

## Meeting-State Integration

The existing meeting tables already have the desired shape:

- `meeting_attendance(meeting_key, member_slug)`
- `reading_statuses(meeting_key, member_slug)`
- `member_contacts(meeting_key, member_slug)`

Implementation should preserve that shape and tighten callers around it:

- `request_roll_call_update` must target only members missing a row in `meeting_attendance` for the meeting.
- `request_reading_update` must target only confirmed attendees who are not already finished or on track.
- Email replies and Discord commands should both write the same rows.
- `meeting_campaign.snapshot()` should remain the operational source for who still needs a nudge.
- Historical email import should not backfill current attendance or reading state by default. It is archive context, not an operations replay, unless we deliberately run a separate recovery script.

## Privacy and Storage

The archive should remain local/private with Oliver's SQLite database.

Implementation notes:

- Do not commit `agent/oliver.db` or imported mail data.
- Consider excluding `body_html` at first unless the export proves text conversion loses important content.
- Do not store attachment blobs or extracted attachment text in v1. A compact attachment manifest in `mail_messages.attachments_json` is acceptable if it falls out of the MIME parse, but attachment search is a future enhancement.
- Keep a clear backup story for `agent/oliver.db`, since the imported archive will become operationally valuable.
- Keep snippets in tool outputs short by default.
- Do not expose archive contents through the public website build.

## Implementation Phases

### Phase 1: Schema and Identity Audit

- Add archive tables and indexes to `agent/db.py`.
- Add helper functions for upserting threads/messages and searching FTS.
- Add participant and participant-address helpers.
- Add `identity_claims` or an equivalent review queue.
- Add `conversation_links` or equivalent source metadata for future Discord/email turns.
- Add tests for email normalization, idempotent upsert, and member resolution.
- Add a small diagnostic command or script to list unresolved email addresses.

### Phase 2: Historical Import Dry Run

- Build an importer for the exported format.
- Run dry-run reports without writing.
- Review unresolved sender mappings.
- Accept safe aliases into `member_emails`.
- Confirm the importer uses `X-GM-THRID` for `tmp/topics.mbox` thread IDs.
- Confirm Google Groups footer/quote cleaning produces useful snippets.

### Phase 3: Historical Import Write

- Import messages into `mail_messages`, `mail_threads`, `mail_participants`, `mail_participant_addresses`, and FTS.
- Verify counts, thread ranges, duplicate handling, and sample thread rendering.
- Back up the SQLite database before and after import.

### Phase 4: Live Archive Capture

- Archive each allowed live mailing-list message before reply/no-reply decision.
- Link `inbound_emails.email_id` to `mail_messages.processed_inbound_email_id`.
- Preserve existing ignored-email logging.
- Add regression tests proving ignored mailing-list messages are still archived.

### Phase 5: Oliver Retrieval Tools

- Add `search_mail_archive`.
- Add `get_mail_thread`.
- Update the system prompt to explain when to use mail archive tools.
- Fix `search_discussion` channel labeling so email-style channel IDs are safe.

### Phase 6: Summaries and Durable Memory

- Add thread summaries for long threads.
- Optionally add yearly or topic summaries after the raw archive is stable.
- Add provenance when Oliver saves memories from mail-derived facts.
- Add tests that the same `member_slug` memory appears from Discord and email contexts.

### Phase 7: Unified Search Facade

- Add a single cross-surface search tool once the separate Discord and email paths are reliable.
- Return surface-tagged results from Discord conversations, email archive, and memories.
- Keep existing specialized tools for cases where Oliver needs one surface only.

## Tests and Verification

Minimum tests:

- Importer is idempotent.
- Duplicate `Message-ID` does not duplicate rows.
- Messages without `Message-ID` receive stable hashes.
- `X-GM-THRID` becomes the thread key for `tmp/topics.mbox`.
- Known member email resolves to the expected `member_slug`.
- Known archive aliases resolve to the expected `member_slug`.
- Unknown historical participant creates a participant/address record and, when appropriate, an identity claim. It does not create a current meeting obligation.
- Recipient-only addresses remain in message header JSON and do not create participant/member records.
- HTML-only messages produce searchable text.
- Google Groups footers and quoted history are excluded from indexed snippets when practical.
- Attachment blobs and extracted attachment text are not stored in v1.
- Messages with attachments still import their email body and optional compact attachment manifest.
- Live mailing-list message is archived even when Oliver decides not to reply.
- Attendance recorded from Discord suppresses later email roll-call targeting for that member.
- Attendance recorded from email is visible to Discord meeting tools.
- `search_mail_archive` returns expected snippets.
- `get_mail_thread` returns chronological messages.
- `search_discussion` does not crash on non-numeric channel IDs.

Live verification after implementation:

- Back up `agent/oliver.db`.
- Run importer dry run and review stats.
- Run importer write mode.
- Confirm imported counts for `tmp/topics.mbox`: 2,441 messages and 538 Gmail threads.
- Query counts and sample threads directly from SQLite.
- Query alias mappings for Loren, Erik, Jamie, and Tom historical addresses.
- Restart Oliver through `agent/script/admin.sh restart`.
- Confirm `agent/script/admin.sh status`.
- Send or receive one harmless mailing-list test message.
- Confirm it appears in `mail_messages`.
- Confirm ignored-message logging still appears in Oliver log/activity.

## Open Questions

- The supplied historical export is mbox, specifically `tmp/topics.mbox`.
- Should we store raw HTML, or only cleaned text plus headers? The mbox has 166 HTML-only messages, so at minimum the importer needs HTML-to-text conversion.
- Should direct member-to-Oliver emails be archived in the same tables, or only mailing-list mail?
- Do we want a manual review command for accepting `identity_claims` into `member_emails`?
- Should accepted historical aliases be mirrored into `member_emails`, or only stored in `mail_participant_addresses`?
- How aggressively should quoted text and list footers be stripped?
- Should attachment indexing be added later if there is a concrete retrieval miss around picker decks, survey PDFs, or book notes?
- Should thread summaries be generated during import or lazily on first retrieval?
- What is the backup/restore plan for the enlarged SQLite database?
