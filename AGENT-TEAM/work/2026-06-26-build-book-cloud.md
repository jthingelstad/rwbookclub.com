# Build Plan — Book Cloud capture + retrieval (P1)

**Date:** 2026-06-26
**Role:** Build Manager
**Upstream:** `2026-06-25-product-horizon-cloud-feedback.md` (PM P1 slice + D1)
**Task (one sentence):** Turn the PM's P1 Book Cloud slice into a file-level
implementation plan, propose the `book_cloud` table DDL, and draw the slice
boundary the runtime actually enforces.

> Not implemented in this run. This is a plan + DDL proposal per the routine
> contract (plans, not edits, unless the invocation asks for code). Schema shape
> is proposed for sign-off before any coding starts.

---

## The one finding that reshapes the slice

The PM slice assumes "passive capture" of every book mention in Discord and the
mailing list. The runtime does not give us that for free. In a monitored channel,
an **unaddressed** message is only logged — the agent loop never runs:

```python
# agent/bot.py:514-516
if not _is_addressed(is_mention, has_name, await _is_reply_to_bot(message)):
    if cid in MONITORED:
        db.log_message(str(cid), "user", content.strip(), speaker=message.author.display_name)
    return                     # ← no oliver.answer(), so no tool can fire
```

A model-invoked tool (`book_cloud_add`) can therefore only capture on turns
Oliver **already runs**: everything in `#ask-oliver`, addressed messages in the
main channel, inbound email Oliver answers, and the mailing-list decision turn
(`answer_mailing_list_email`, which runs the full loop even when it ends in
`[[NO_REPLY]]`). The highest-value case the PM cites — members chatting *among
themselves* and dropping a title — is exactly the unaddressed case the loop
skips. PM acceptance criteria #2 ("a bare mention produces no reply") and #6
(mailing-list parity) only fully hold once a passive pass exists.

So the honest decomposition is two slices, and only the first is "ready to code":

- **Slice 1a (this plan) — storage + tools + in-turn capture instruction.**
  Captures on every turn Oliver runs. Pure private SQLite, no new invocation
  path, no per-message cost. Smallest shippable, lowest risk.
- **Slice 1b (Build→Product handoff) — passive extraction of unaddressed
  mentions.** A cheap Haiku extraction at `log_message` time for monitored
  channels + silently-read list mail. Adds a per-message LLM cost and a
  precision problem (junk rows) that needs an Evaluator gate and a Jamie cost
  call. Specified below as a sketch, **not** scheduled here.

This is the Product→Build→Product pattern from the team README: 1a ships the
value that's unambiguous; 1b surfaces a real product/cost decision before we
spend tokens on every chatter message.

---

## Goal

Capture books mentioned during turns Oliver handles (Discord + email) as a
private, structured book cloud, and let members retrieve it conversationally
("what have we been circling lately?"). Private SQLite only — no corpus or
website writes (PM D1).

## Files Likely Touched

| File | Change | Notes |
|---|---|---|
| `agent/db.py` | Add `book_cloud` table to `_SCHEMA`; add `add_book_cloud_entry()` + `recent_book_cloud()` helpers | Follows the existing `CREATE TABLE IF NOT EXISTS` + short-lived-connection helper pattern. No `_migrate()` entry needed (new table, created idempotently on import). |
| `agent/tools.py` | Add two tool schemas (`book_cloud_add`, `book_cloud_recent`) to `TOOLS`; add two `dispatch()` branches | Append to end of `TOOLS` so the cached tool prefix stays stable (the file's stated invariant). Mentioner/surface/channel come from `ctx`, never model input. |
| `agent/oliver.py` | Add a `BOOK CLOUD` paragraph to `OPERATIONAL_PROMPT` | The capture *policy* (silent, genuine references only, reason is mandatory) lives here; the *rule* that the mentioner can't be spoofed lives in code (dispatch reads `ctx`). |
| `tests/test_db.py` | Round-trip + ordering + retrieval tests for the two helpers | Matches existing `test_db.py` style; uses the scratch DB from `conftest.py`. |
| `tests/test_tools_dispatch.py` | `book_cloud_add` resolves mentioner from `ctx` (not input); `book_cloud_recent` returns a list; bad input → error dict | Matches existing dispatch happy/err-path style. |
| `agent/README.md` | One line under Tools listing `book_cloud_add` / `book_cloud_recent` | Docs-update-when-behavior-changes (role anti-pattern: treating docs as optional). |

No `website/`, no `corpus/`, no `gitwrite.py`. Confirmed against PM acceptance #5.

## Proposed `book_cloud` schema (sign-off needed before coding)

Mirrors PROCESS.md "for each mention capture: title, author, who, when, where,
why, related," and the db.py conventions (`id` autoincrement, ISO `created_at`,
slug references, nullable provenance).

```sql
CREATE TABLE IF NOT EXISTS book_cloud (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    title             TEXT NOT NULL,         -- mentioned book title (free text; NOT required to be a corpus book)
    author            TEXT,                  -- when known
    book_slug         TEXT,                  -- set only on a confident match to a corpus book; else NULL
    mentioned_by      TEXT,                  -- member slug, resolved from ctx via the identity map; NULL if speaker unlinked
    mentioned_by_name TEXT,                  -- display-name fallback, for provenance only (never the identity of record)
    surface           TEXT NOT NULL,         -- 'discord' | 'email' | 'mailing_list'
    channel_id        TEXT,                  -- channel id, or 'email:<addr>' / thread key — where it came up
    source_message_id TEXT,                  -- back-reference to the originating message/email
    reason            TEXT NOT NULL,         -- WHY it came up — the cultural payload; a generic "mentioned in chat" is a failure
    reason_kind       TEXT,                  -- coarse tag (see taxonomy below) — Ethnographer to confirm the set
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_book_cloud_created ON book_cloud(created_at);
CREATE INDEX IF NOT EXISTS idx_book_cloud_title   ON book_cloud(title);
```

Helper signatures (db.py):

```python
def add_book_cloud_entry(*, title: str, reason: str, surface: str,
                         author: str | None = None, book_slug: str | None = None,
                         mentioned_by: str | None = None, mentioned_by_name: str | None = None,
                         channel_id: str | None = None,
                         source_message_id: str | None = None) -> int: ...

def recent_book_cloud(*, limit: int = 20, query: str | None = None) -> list[dict]: ...
    # newest first; `query` is an optional LIKE over title/author/reason
```

Design decisions baked in (all from the PM brief, called out so review is cheap):
- **No dedupe.** Same book mentioned twice = two rows. The *reason* is the unit
  of value (PM non-goal + acceptance #4). `add_*` is an unconditional INSERT.
- **`reason` is `NOT NULL`.** Enforces the cultural payload at the storage layer,
  not just the prompt (role anti-pattern: prompt-only business rules). A capture
  with no reason should be impossible to persist.
- **Mentioner is `ctx`-derived, never model input.** `book_cloud_add`'s schema
  does **not** expose a mentioner field; dispatch fills `mentioned_by` from
  `ctx["member_slug"]` and `mentioned_by_name` from `ctx["speaker"]`, exactly as
  `remember`/`record_availability` do. This is acceptance #1 ("resolved via the
  identity map, not display name") enforced in code.
- **`reason_kind` is optional + advisory** until the Ethnographer signs off the
  taxonomy. Proposed starting set: `nomination | comparison | objection |
  recommendation | side_reference | joke`. Not gated by a CHECK constraint so the
  set can evolve without a migration.

## Implementation Steps

1. **db.py** — add the table to `_SCHEMA` and the two helpers near the
   `# ── Memories ──` block (it is the closest cousin). New table ⇒ no
   `_migrate()` change.
2. **tools.py** — append two schemas to `TOOLS`:
   - `book_cloud_add`: `{title (req), reason (req), author?, reason_kind?}`.
     Dispatch derives `surface` from `ctx` (`'mailing_list'`/`'email'` when
     `channel_id` starts with `email:`, else `'discord'`), `mentioned_by` from
     `ctx["member_slug"]`, `channel_id`/`source_message_id` from `ctx`. Returns
     `{"saved": True, "id": n}`. Silent by construction (no member-visible side
     effect; Oliver still replies to the actual question in the same turn).
   - `book_cloud_recent`: `{limit?, query?}` → `recent_book_cloud(...)`. Clamp
     `limit` to `[1, 50]` like the other read tools.
3. **oliver.py** — add to `OPERATIONAL_PROMPT` (after the `IN THE ROOM`
   paragraph, where `remember` is already introduced):
   > BOOK CLOUD. When a member genuinely references a book — naming it, comparing
   > it, recommending it, objecting to it — quietly record it with `book_cloud_add`,
   > capturing *why it came up* in `reason` (the connection is the point; "mentioned
   > in chat" is not a reason). This is silent bookkeeping: never reply, ask a
   > follow-up, or interrogate intent just because a book was named. A reference is
   > not a nomination unless the member says so. To answer "what have we been
   > circling lately?" use `book_cloud_recent` and frame the result as books
   > orbiting the conversation — not a queue, ranking, or commitment.
4. **Tests** — per the test plan below.
5. **Docs** — one line in `agent/README.md` Tools list.

## Tests / Evals

Deterministic unit tests (this slice; no model calls — keep the suite ~0.6s):

- `test_db.py`
  - `add_book_cloud_entry` returns an id; row round-trips with all fields.
  - `recent_book_cloud` is newest-first and honors `limit`.
  - Same title twice (different `reason`) ⇒ **two** rows, both returned
    (acceptance #4 at the storage layer).
  - `query` filters across title/author/reason.
- `test_tools_dispatch.py`
  - `book_cloud_add` with `ctx={"member_slug":"tom","speaker":"Tom",...}` writes
    `mentioned_by="tom"`; a model `input` cannot set the mentioner (schema has no
    such field — assert the persisted row used `ctx`).
  - `surface` derives to `mailing_list`/`email` when `ctx["channel_id"]`
    starts with `email:`, else `discord`.
  - `book_cloud_add` missing `reason` ⇒ error dict (NOT NULL surfaces as a caught
    exception, same pattern as the existing `get_book` missing-arg test).
  - `book_cloud_recent` returns a list.

Behavioral evals — **Evaluator owns**, not in this slice (flagged, not skipped):
- Capture **precision** golden set: real chatter with and without genuine book
  mentions, judged on "no junk rows" over recall (PM risk #1). This gates 1b more
  than 1a, but the in-turn capture in 1a should also be measured for over-capture.
- Retrieval **voice/grounding** case: "what have we been circling?" judged on
  reasons-present + "orbit not queue" phrasing (PM acceptance #3).

## State / Migration Notes

- New table, created by `CREATE TABLE IF NOT EXISTS` on import — no migration
  ordering, consistent with every other table here. Nothing to backfill.
- `oliver.db` is gitignored private state (class B). No corpus/website effect.
- **Adding a tool invalidates the prompt cache prefix once** (tools render before
  system). Expected and harmless — one cache-miss turn on deploy.
- Mailing-list archive backfill (2,445 messages) is explicitly **out of scope**
  and flagged, not silently skipped (PM non-goal). It would be a one-shot import
  needing its own precision pass; revisit after the live cloud proves the shape.

## Rollout Notes

- Pure additive: no behavior change to existing surfaces, no scheduler change, no
  external action (no email/DM/post). The capture tool has no member-visible
  output; retrieval only fires when a member asks.
- Ship behind nothing — there's no destructive path to gate. If capture proves
  noisy in practice, the mitigation is prompt-only (tighten the BOOK CLOUD
  paragraph) or stop registering the tool; no data cleanup needed since rows are
  inert private notes.
- Rollback = revert the three code files; the orphan table is harmless and can be
  left in place.

## Risks

- **Coverage gap is real, not theoretical.** 1a captures only on turns Oliver
  runs. Set expectations with Jamie: until 1b lands, the cloud reflects
  Oliver-involved moments, not all club chatter. *Mitigation:* state it plainly
  in the handoff; don't let "book cloud shipped" imply full passive capture.
- **Over-capture.** The model may log every title-shaped phrase. *Mitigation:*
  "genuine reference" + mandatory `reason` in the prompt; Evaluator precision
  gate; rows are cheap and reversible if it's noisy.
- **Silent-capture vs. the "always reply" rule.** `OPERATIONAL_PROMPT` already
  says "after any tool calls, always compose a reply." `book_cloud_add` fits
  cleanly — it's one tool mid-turn and Oliver still answers the real question.
  The genuinely silent case (capture with *no* reply) only arises for unaddressed
  messages, which is 1b. No conflict in 1a.
- **`reason_kind` taxonomy is a culture call**, not an engineering one — gated on
  Ethnographer sign-off; shipped nullable so it can't block 1a.

---

## Handoffs

### To Club Ethnographer (blocking the `reason_kind` set; not blocking 1a)

**Decision Needed:** Confirm or revise the `reason_kind` taxonomy (`nomination |
comparison | objection | recommendation | side_reference | joke`) and approve the
"orbit not queue" retrieval phrasing in the BOOK CLOUD prompt paragraph.
**Constraints:** PROCESS.md — capture is silent, a mention is not a nomination.
**Proposed next step:** A short note on the taxonomy; `reason_kind` ships nullable
so 1a can land before the taxonomy is final.

### To Evaluator (owns the behavioral gate; parallel to 1a)

**Decision Needed:** A capture-**precision** golden set (junk-row avoidance over
recall) and a retrieval voice/grounding case for acceptance #3.
**Constraints:** Private SQLite only; no corpus/website assertion in scope.
**Proposed next step:** Stand the golden set up against `book_cloud_add`'s in-turn
behavior now; reuse it as the gate for 1b before any passive pass ships.

### To Product Manager (Build→Product — the 1b decision)

**Context:** Passive capture of unaddressed mentions requires a new extraction
path at `log_message` time (monitored channels + silently-read list mail), which
adds a per-message LLM cost and a precision problem.
**Decision Needed:** Is 1b in scope, and at what cost ceiling? Options: (a) Haiku
extraction on every unaddressed monitored message; (b) a periodic batched sweep
over recent unlogged-as-cloud messages (cheaper, laggier); (c) defer 1b until the
in-turn cloud proves the shape. Build recommends (c) then (b).
**Constraints:** Cost-conscious mandate (oliver.py model strategy); capture must
stay silent; identity via the map, not display name.
**Proposed next step:** PM picks a 1b option (or defers); Build writes the 1b plan
only once the cost ceiling and trigger are explicit. The horizon-awareness slice
(PM #2) remains the other open P1 and is independent of this one.

## Slice 1b sketch (for the PM decision; not scheduled)

- New `db.recent_unprocessed_messages()` or a `book_cloud_scanned` watermark so a
  sweep doesn't re-scan.
- A `oliver.extract_book_mentions(text) -> list[{title, author?, reason, kind?}]`
  Haiku, tool-less, returns `[]` for the common no-mention case (the precision
  lever). Reuses `add_book_cloud_entry`.
- Trigger: batched sweep (option b) over messages since the watermark, on the
  existing scheduler tick — no new always-on path, amortized cost, and dedupe via
  the watermark. Per-message extraction (option a) is simpler but costs on every
  chatter message; Build prefers (b).
- Same `surface`/identity resolution as 1a, so storage and retrieval are
  unchanged — 1b is purely a new *capture source*, not a new schema.
