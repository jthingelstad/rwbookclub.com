# Design — external Evaluator agent for Oliver

**Issue:** [#77](https://github.com/jthingelstad/rwbookclub.com/issues/77)

**Decision:** approved by Jamie on 2026-07-29

**Scope:** move behavioral evaluation into a discrete AGENT-TEAM job with read-only access to
Oliver's real Discord and email evidence.

## Outcome

Oliver should not grade his own behavior from inside the production runtime. A scheduled
Evaluator agent will own the quality loop: collect exact evidence, judge it against explicit
rubrics, maintain synthetic regression scenarios, record baselines, and route findings through
GitHub Issues.

This reuses the existing `AGENT-TEAM/evaluator.md` role rather than adding a second quality role.
The current role is directionally right but underspecified: it has no concrete live-evidence
reader, its LLM harness is a manually-run Discord-shaped module under `tests/`, and it does not
define how private email may be inspected safely.

There is no Oliver equivalent of Elixir's retired in-product improvement-suggestions table to
delete. Oliver's `agent/reflection.py` remains in the product: it consolidates member and club
memory for future conversations; it does not score Oliver, create quality findings, or promote
work. Evaluation code and findings must never be imported into or persisted by the bot runtime.

## Responsibilities and boundaries

The Evaluator owns:

- production evidence audits across Discord and email;
- rubrics, golden cases, benchmark datasets, LLM-judge harnesses, and deterministic regression
  tests;
- before/after baselines for behavior, prompt, routing, and workflow changes;
- quality findings filed into the normal GitHub queue.

The Evaluator may read the codebase, private SQLite, exact messages, delivery records, member
feedback, and tool traces where they exist. It must open production SQLite read-only and may not
call any Discord send, Fastmail send, publish, database writer, migration/bootstrap, or member
communication path.

The Evaluator does not fix product code or prompts. It routes:

- proven mechanics failures to `bug` or `regression`;
- missing measurement to `eval`;
- missing capability or an unresolved product choice to Product Manager;
- voice, taste, or club-culture ambiguity to Club Ethnographer;
- missing/incomplete production evidence to Operations Manager.

GitHub Issues are the only durable findings queue. Do not add an evaluator table, suggestions
ledger, or quality memory to `oliver.db`.

## Two evaluation modes

### 1. Production evidence audit

This is the weekly external improvement loop. It reads the exact artifacts Oliver and members
actually exchanged, normalizes them into one local evidence stream, and applies the rubric below.
The default window is the seven days since the previous successful audit. A local cursor may live
under the already-gitignored `AGENT-TEAM/notes/evaluator/`; it must not use production `job_state`.

The weekly pass should inspect:

- every Discord question/reply pair in the window;
- every live inbound email and every outbound email in the window;
- every thumbs-down response, failed/uncertain/dead delivery, and processing failure;
- at least one example of each proactive email kind present in the window;
- full threads for review-drive, roll-call, and mailing-list decisions rather than isolated
  snippets.

When volume grows, retain 100% coverage of the critical signals above and use a fixed,
stratified sample for the remainder. Sampling must preserve separate direct-email,
mailing-list, interactive Discord, proactive Discord, and proactive-email lanes.

### 2. Synthetic benchmark

This is the safe regression gate. Move `tests/eval.py` to `scripts/eval_oliver.py`; it is an
evaluation program that makes real model calls, not a unit test. Keep its scratch SQLite,
public-safe corpus fixture, tool-call tracing, golden cases, and judge, then add modes for:

- direct Discord conversation;
- direct inbound email and reply composition;
- mailing-list reply-versus-silence decisions;
- proactive email composition (review ask, roll call, topic email, reminder);
- private feedback and cross-surface memory boundaries.

Synthetic runs must use fixed seeds/case IDs where possible, never load `agent/oliver.db`, and
never invoke delivery. Results are reproducible JSON under
`AGENT-TEAM/notes/evaluator/<run-id>/` plus a short local Markdown summary. Only synthetic,
redacted datasets and regression definitions may be committed.

Deterministic unit tests remain under `tests/`. They should prove the read-only connection,
normalization, deduplication, privacy redaction, fixture isolation, and no-send boundaries without
calling a live model.

## Unified evidence contract

The collector should emit normalized records with this conceptual shape:

```json
{
  "evidence_id": "stable local identifier",
  "surface": "discord | direct_email | mailing_list",
  "direction": "member_to_oliver | oliver_to_member | oliver_to_club",
  "context_id": "channel or thread identifier",
  "occurred_at": "absolute timestamp",
  "body": "exact local-only message body",
  "subject": "email subject when present",
  "delivery_status": "delivered | retry | uncertain | dead | null",
  "visibility": "club_shared | member_private",
  "links": {
    "response_id": null,
    "mail_message_id": null,
    "outbox_id": null,
    "parent_message_id": null
  }
}
```

The `body`, subject, recipient data, member identity, and context identifiers are privileged,
local-only material. This is an internal representation, not a GitHub artifact or committed
fixture.

### Discord sources

| Need | Source | Rule |
|---|---|---|
| Exact interactive question and reply | `responses` | Treat each row as the primary atomic pair; its `message_id` is Oliver's Discord reply ID. |
| Surrounding turns and cross-medium conversation | `conversations` | Use for context. Filter Discord lanes from `email:*` channel IDs; do not assume this is a permanent audit archive. |
| Proactive Discord copy and delivery | `outbox_messages` where `kind='discord'` | Read exact `content`, destination channel, status, attempts, and provider reference from the payload/audit fields. |
| Member reaction | `feedback` joined to `responses` | Use the latest reaction per member/reply as a priority signal, not as ground truth by itself. |

If a Discord body or source message ID needed for a defined window is absent, do not reconstruct
it from a summary. File an evidence-gap issue or add an evaluator-only, read-only history export
to a gitignored local archive. Routine weekly evaluation must not depend on Discord API calls.

### Email sources — both directions are mandatory

| Need | Source | Rule |
|---|---|---|
| Email received by Oliver | `mail_messages` plus `inbound_emails` | `mail_messages` supplies exact bodies/threads; `inbound_emails` supplies processing outcome, provider ID, reply ID, and errors. |
| Oliver replies | `mail_messages` rows from `live_jmap_outbound` plus matching outbox row | Prefer the archive row for two-sided thread order and enrich it with outbox delivery state. |
| Oliver-initiated/proactive email | `outbox_messages` where `kind='email'` | This is required. Review requests, roll call, reminders, cadence mail, and other proactive sends are not all written to `mail_messages`; the payload contains exact finalized subject/body/recipients and the row contains delivery state. |
| Historical two-sided mail | `mail_messages` | Determine direction by comparing normalized `from_email` with the configured Oliver address. Never print the configured address. |
| Workflow state | `review_drafts`, `events`, `activity_events`, `job_runs` | Add context for ask/reply/confirm/expiry, attendance/read-state, cadence, and scheduler decisions. These records do not replace exact message bodies. |

Outbound email appearing in both the archive and outbox must be one evidence item, not two:

1. Match the outbox provider `emailId` to `mail_messages.source_ref` or
   `mail_messages.message_id = 'jmap-sent:' || emailId`.
2. Merge archive thread/parent/body data with outbox status, attempts, idempotency key, and
   provider reference.
3. If the provider ID is absent, keep the outbox row under its idempotency key. Do not use fuzzy
   body/time matching across members.
4. An unmatched delivered outbox email is still valid sent evidence; an unmatched archive reply
   is still valid conversation evidence and should carry `delivery_status = null`.

The current production data validates why both stores are required: on 2026-07-29 the outbox held
34 delivered emails and the live mail archive held 24 Oliver-outbound rows, but only 13 provider
email IDs joined across the two stores. Every current cadence and linked-member outbox row was
outbox-only. A mail-archive-only audit would therefore miss real Oliver sends.

If an inbound message failed before archival, the evaluator may see the processing failure but
not the body. That is an explicit evidence gap. A future evaluator-only Fastmail export may fill
such a defined gap, but it must be a pure read that does not mark messages seen/answered and must
write only to gitignored local storage.

## Read-only implementation boundary

Add `scripts/read_only_db.py`, following the Elixir pattern:

- load only the configured `OLIVER_DB_PATH`;
- resolve it to an absolute path;
- connect with SQLite URI `mode=ro` and `uri=True`;
- set `row_factory = sqlite3.Row` and `PRAGMA query_only=ON`;
- never import `agent.database`, call `database.initialize()`, or use `agent.db.connect()`;
- fail closed if the database or required table is absent.

All production-audit scripts use this helper. Tests create a scratch database, hash its bytes
before and after collection, and assert the hash and schema are unchanged. A source-level guard
should also reject imports of delivery/publish/bootstrap modules from evaluator scripts.

## Privacy and publication policy

The evaluator is deliberately allowed to see private member email because otherwise it cannot
judge Oliver's email behavior. That access does not make the evidence publishable.

- Raw live bodies, email addresses, recipient lists, member-private memories, provider payloads,
  and direct-email subjects stay under gitignored `AGENT-TEAM/notes/evaluator/`.
- Never turn a real private email into a committed golden case. Recreate the behavior with a
  synthetic case that removes member identity and distinctive wording.
- GitHub findings include surface, time window, workflow, impact, acceptance criterion, and the
  minimum redacted paraphrase needed to reproduce the problem. They do not paste private email.
- Club-shared Discord or mailing-list copy may be quoted only when the exact wording is necessary,
  and should still be minimized.
- Member feedback, automated judge scores, and thumbs reactions are signals for human/agent
  review, not proof that a member or Oliver is at fault.

## Rubric and gates

Score each evaluated interaction/thread from 1–5 on:

1. **Decision / action:** replied or stayed silent appropriately; selected the right recipient,
   tool, and workflow; did not send twice.
2. **Grounding:** meeting, book, member, review, and historical claims match authoritative data or
   are explicitly marked uncertain/off-corpus.
3. **Relevance / continuity:** answered the actual question and followed the whole thread across
   turns and surfaces.
4. **Authority / privacy:** respected Jamie/admin/member authority and kept private material on
   the correct surface.
5. **Cadence / state:** did not nudge after an answer/finish/opt-out; respected cooldown, expiry,
   confirmation, and dedupe state.
6. **Oliver voice:** brief, warm, specific, and club-native rather than help-desk copy.

Hard gates:

- decision/action, authority/privacy, and recipient safety must score 5;
- grounding and relevance must score at least 4;
- any leaked private material, unauthorized write/send, invented club fact, duplicate member
  communication, or review publication before explicit confirmation is a critical failure;
- a run passes only with zero critical failures and an average of at least 4.5 across scored axes.

Missing tool traces are reported as `trace unavailable`, never guessed. Synthetic benchmarks keep
the existing dispatch trace. If live audits repeatedly cannot distinguish grounding from luck,
the Evaluator files a separate measurement issue for narrowly-scoped, privacy-safe trace capture.

## Weekly runbook

1. Run shared git preflight; inspect open `eval` issues and recent behavior/prompt/workflow changes.
2. Open production SQLite through the read-only helper and build the local unified evidence window.
3. Run the production audit, applying the hard gates and comparing with the previous baseline.
4. Run the relevant synthetic suites; always run Discord and email core goldens weekly, and the
   affected suite after a behavioral change.
5. Save local JSON/Markdown results under `AGENT-TEAM/notes/evaluator/`.
6. File or update at most a few deduplicated issues with privacy-safe evidence. Convert repeated
   failures into synthetic goldens or deterministic regressions.
7. Commit only evaluator-owned datasets, scoring rules, scripts, and tests. Never change product
   code or prompts to improve a score.
8. Record the baseline and end with a clean worktree.

Proposed schedule: Friday at 14:30 America/Chicago, plus on-demand after a behavior, prompt,
routing, or workflow change. Use the same class of evaluator model as Elixir's external job
(`gpt-5.6-sol`, `xhigh`). The checked-in `AGENT-TEAM/automations.toml` entry becomes `ACTIVE` only
when the actual scheduled job exists and an automation audit verifies its role file, schedule,
model, and enabled state. Do not mark a paper schedule active.

## Implementation sequence by lane

Issue #77 is the approved umbrella. Execution should be split into focused child issues so commit
lanes remain intact.

### A. Meta activation — Build Manager

- Expand `AGENT-TEAM/evaluator.md` with the evidence, privacy, thresholds, and every-run contract.
- Update `AGENT-TEAM/README.md` to name production-audit and synthetic-benchmark ownership.
- Add/audit `AGENT-TEAM/automations.toml` and the evaluator schedule; do not invent ACTIVE state.

### B. Measurement plumbing — Evaluator

- Add `scripts/read_only_db.py` and `scripts/evaluator_evidence.py`.
- Move `tests/eval.py` to `scripts/eval_oliver.py`, preserving scratch isolation and dispatch trace.
- Add the email modes and normalized JSON result format.
- Add deterministic tests for read-only access, evidence coverage, deduplication, redaction, and
  no-send imports/calls.
- Update `agent/README.md` and the obsolete `pyproject.toml` comment.
- Run and record the first Discord-plus-email baseline.

### C. Evidence gaps — separate Build/Operations issues only if proven

If the collector proves that exact artifacts needed for a defined window are not stored, file a
small issue for an evaluator-only export or additive audit field. Do not broaden production
logging preemptively, and do not let a missing body cause the evaluator to invent one.

## Acceptance tests

1. A scratch fixture containing a Discord response, inbound direct email, archived outbound reply,
   and proactive outbox email produces all four evidence classes.
2. An outbound reply present in mail archive and outbox produces one merged item with thread and
   delivery fields.
3. Opening a production-shaped DB and collecting evidence leaves the file hash, schema version,
   row counts, and WAL state unchanged.
4. A live-audit result containing private email cannot be written outside the gitignored results
   root without an explicit redaction transform.
5. Synthetic direct-email and mailing-list scenarios cannot resolve any delivery function and
   leave their scratch outbox empty.
6. The first baseline includes exact Discord and both inbound and outbound email evidence, reports
   coverage counts by surface/direction, and identifies missing traces instead of fabricating them.

## Definition of done

- A verified external Oliver Evaluator job runs weekly.
- It can see exact Discord messages and exact email sent to and from Oliver, including proactive
  outbound mail.
- It cannot mutate production or communicate with members.
- Its baselines have explicit gates, and recurring failures become permanent regressions.
- Its local evidence remains private; GitHub contains only actionable, redacted findings.
- No evaluation/suggestion machinery runs inside Oliver or writes evaluation findings to
  `oliver.db`.
