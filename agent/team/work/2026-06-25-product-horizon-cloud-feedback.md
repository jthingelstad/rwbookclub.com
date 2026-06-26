# Product Brief — Horizon, Book Cloud, and the Private-Feedback Boundary

**Date:** 2026-06-25
**Role:** Product Manager
**Task (one sentence):** Resolve the three product decisions handed to the PM
today — book-cloud privacy/shape, nudge-cadence re-grounding, and where private
review feedback lives — and set a priority order grounded in what is actually
built.

This brief responds to three same-day handoffs:

- `2026-06-25-ethnography-baseline.md` → "decide whether book-cloud entries are
  private SQLite, public corpus, or private with an export path; write a small
  capture/retrieval slice."
- `2026-06-25-ethnography-rhythm-and-ritual.md` → "re-ground nudge cadence to the
  club's real ~6/year rhythm; make a thinning horizon trigger an offer to pick
  in-channel."
- `2026-06-25-evaluator-voice-layer.md` (F3) → "product call on where private
  qualitative feedback is stored vs. the public review path."

---

## The load-bearing finding the handoffs circle

Two of Oliver's three most-stated product promises are **specified but unbuilt**:

| Promise (SOUL/PURPOSE/PROCESS) | Runtime reality (verified today) |
|---|---|
| "Oliver should always know the next five books." | **No horizon concept exists.** `corpus_read.upcoming_meetings()` lists *scheduled* placeholder books, but nothing maps the host rotation (Erik→Jamie→Loren→Nick→Tom) to "whose next pick is missing," and there is no horizon nudge in `scheduler.py` — only a meeting reminder and a review nudge. |
| "Track books members mention — the book cloud." | **No book-cloud surface exists.** No table in `db.py`, no capture path, no retrieval tool. The 3-row `memories` table could hold notes loosely but has no structure for title/author/who/when/why/thread. |
| "Private feedback ≠ public review." | **No private-feedback path exists.** The `/oliver review` modal writes every field (rating, DNF, discussionQuality, wouldRecommend, favoriteQuote) to the *public* corpus file. There is no place a private taste signal can land. |

This reframes the handoffs. The rhythm note asks to *re-cadence the horizon nudge*
— but the nudge doesn't exist, so the real work is **horizon awareness**, not
cadence tuning. The baseline note asks where the book cloud lives — but nothing
captures it yet, so the decision and the first slice are the same work. The
evaluator's F3 asks where private feedback goes — but there is no private bucket
at all.

Good news: one already-correct thing. The **meeting reminder is already anchored
to the actual scheduled date** (`scheduler.py` computes days-to-`meetingDate`,
not a calendar month), so the ethnographer's "stop assuming monthly" worry is
already satisfied for reminders. The cadence problem is confined to *future*
horizon nudges, which we get to design from scratch — cleanly.

---

## Decisions (answering the three handoffs)

**D1 — Book cloud is private SQLite, retrievable on demand, with NO automatic
public export.** A dedicated `book_cloud` table in `db.py` (private operating
state, per the Class-B/Class-A split in ROADMAP.md). Members can ask "what have
we been circling?" and Oliver answers from it. Publishing any of it to the corpus
or website is a **separate, later, Jamie-authorized slice** — not in scope now.
This honors SOUL ("private operational state belongs in Oliver's memory") and the
guardrail that private signals don't auto-become public website content.

**D2 — Build horizon *awareness* before horizon *nudging*; re-cadence is a
doc-and-later-code concern.** The first slice makes Oliver able to *answer*
"what are the next five / whose pick is missing?" Nudging (with the re-grounded
cadence) is a deliberately separate second slice. When nudging is built, its
cadence rules are: anchor to the next scheduled meeting; treat placeholder dates
as soft; read a long gap as momentum risk; monthly while a slot is >90 days out,
weekly inside 90 days and unset — exactly the ethnographer's re-grounding, but
applied to code that doesn't exist yet, so no migration is needed. The
"offer to pick in-channel when the horizon thins" behavior rides on the awareness
slice (Oliver can only offer once it knows the horizon is thin).

**D3 — Private qualitative feedback lands in the existing `memories` table
(`scope='member'`, `subject=<book-slug>`), never in the public review file.** The
`/oliver review` modal stays public-corpus-only and unchanged. A *separate*
capture path (a private-note tool Oliver already has via `remember`, or a
follow-up question after a review) routes taste/DNF-reason/fit signals to
`memories`. DNF reason is the canonical example: the public file records
`dnf: true`; *why* it was a DNF is private recommendation fuel. No schema change
is required — `memories` already has scope/subject/provenance.

---

## Priority order

1. **P1 — Book Cloud capture + retrieval** (this brief, full slice below).
   Highest leverage-to-risk: specified, zero-built, culturally load-bearing,
   fully private (low risk), and it is the *raw material* that makes the horizon
   and any future Picking meeting fast. Ship this first.
2. **P1 — Five-book horizon awareness** (read-only; brief sketched below).
   The core product promise, currently absent. Awareness only — no nudging yet.
3. **P2 — Private-feedback capture** (D3; small, needs the capture path defined).
   Unblocks the evaluator's F3 DNF/private-signal eval scenarios.
4. **P2 — Horizon nudge + cadence + pick-in-channel offer** (depends on #2).
   Where the ethnographer's re-grounded cadence actually lands as code.
5. **P3 — PROCESS.md cadence wording fix** (doc-only): stop asserting "last
   Tuesday" as fact; say "a weekday near month's end, pencilled." Trivial,
   bundle with #4.

---

## Recommended Slice (P1, ready for Build Manager)

### Product Goal

Capture books mentioned in Discord and the mailing list as a private, structured
"book cloud," and let members retrieve it conversationally — so picking and
future Picking meetings start from the club's own remembered orbit of books
instead of a cold start.

### Users / Members Affected

All current members (Erik, Jamie, Loren, Nick, Tom) as askers ("what have we been
circling?") and as the source of mentions. Oliver as the keeper. No public/website
audience in this slice.

### Proposed Behavior

- **Passive capture.** When a book is referenced in Discord or on the mailing
  list, Oliver records a book-cloud entry: title, author (when known), who
  mentioned it, when, where (channel/thread), and *why it came up* (nomination,
  comparison, objection, joke, side reference). Capture is silent — a mention is
  **not** an invitation to interrogate the member (PROCESS.md is explicit).
- **On-demand retrieval.** "What books have we been circling lately?" / "Has
  anyone mentioned X?" returns recent entries with the *reason* and the thread
  they belonged to — framed as "books orbiting the conversation," never a queue,
  ranking, or commitment.
- **Storage.** New `book_cloud` table in `db.py` (private). Entries kept
  indefinitely (they are sparse and their value is the old connection
  resurfacing). A `book_cloud_add` (internal, used during the agent loop when
  Oliver notices a mention) and a `book_cloud_recent` read tool.

### Non-Goals

- No public/corpus/website export of the cloud (separate Jamie-authorized slice).
- No nomination semantics — an entry is not a pick or a candidate unless a member
  says so.
- No follow-up questioning triggered by a mention.
- No backfill of the 2,445-message mailing-list archive in this slice (possible
  later one-shot import; flag it, don't silently skip it).
- No de-dup intelligence beyond "same book mentioned again is a new entry with
  its own reason" (the reason is the unit of value).

### Acceptance Criteria

1. A book mentioned in `#ask-oliver` or the main channel produces exactly one
   `book_cloud` row with title, mentioner (resolved via the identity map, not
   display name), timestamp, channel/thread, and a non-empty `reason`.
2. A bare mention produces **no** reply and **no** follow-up question (silent
   capture); capture is observable only via retrieval.
3. "What have we been circling lately?" returns recent entries *with reasons*,
   phrased as orbit not queue, and says so plainly when the cloud is empty.
4. The same book mentioned twice for different reasons yields two retrievable
   entries, both surfaced.
5. Nothing in this slice writes to `corpus/data/` or the website.
6. Mailing-list mentions are captured under the same rules as Discord (one Oliver,
   two doors).

### Risks / Questions

- **Capture trigger reliability.** Passive capture depends on Oliver *noticing* a
  mention mid-conversation. Risk: over-capture (every title-shaped phrase) or
  under-capture. → Evaluator should own a golden set: real chatter with/without
  genuine book mentions, judged on precision (no junk rows) over recall.
- **Reason quality.** "why it came up" is the cultural payload; a generic
  "mentioned in chat" reason is a failure, not a pass. → Acceptance criterion #3
  and an eval axis.
- **Open question for Jamie (non-blocking):** should the archive backfill happen
  at all, and if so as a one-time import? Default: skip for now, revisit after the
  live cloud proves the shape.

### Recommended Slice

Ship capture + retrieval as private SQLite only (D1). Build Manager: `book_cloud`
table + `book_cloud_add`/`book_cloud_recent` tools + the capture instruction in
the agent loop. Evaluator: precision-focused capture golden set + a retrieval
voice/grounding case. Club Ethnographer: review the `reason` taxonomy
(nomination/comparison/objection/joke/side-reference) and the "orbit not queue"
phrasing before it ships.

---

## Second Slice (P1, sketch — separate brief when scheduled)

**Five-book horizon awareness (read-only).** A `horizon()` computation in
`corpus_read.py` that walks the deterministic host rotation forward from the last
scheduled meeting, pairs each upcoming slot with its scheduled book (from
`upcoming_meetings()`) or marks it **empty**, and exposes it via a `horizon` tool
so Oliver can answer "what are the next five?" and "whose pick is missing?"
Acceptance: correctly identifies the first empty host slot; treats placeholder
dates as soft; no nudging, no writes. This is the prerequisite for both the
nudge slice (#4) and the "offer to pick in-channel" behavior, and it makes the
ethnographer's Picking-meeting insight actionable: a thinning horizon is a
leading indicator Oliver can *see* before it costs the club a meeting.

---

## Handoff

### Context

Three same-day handoffs converged on a single root cause: Oliver's two biggest
product promises (horizon, book cloud) and the private-feedback distinction are
specified but unbuilt. Files: `agent/scheduler.py`, `agent/corpus_read.py`,
`agent/db.py`, `agent/tools.py`, `agent/commands.py`, `corpus/data/reviews/`.

### Decision Needed

Build Manager: turn the P1 Book Cloud slice into an implementation plan (table +
tools + capture instruction + tests). Confirm the `book_cloud` schema shape before
coding. Evaluator: own the capture-precision golden set. Club Ethnographer: sign
off on the `reason` taxonomy and retrieval phrasing.

### Constraints

Private SQLite is canonical for this; no corpus/website writes (D1). Capture is
silent (PROCESS.md). Identity via the Discord/email→member map, not display name.
Jamie authorizes any future public export and any archive backfill.

### Proposed Next Step

Build Manager writes `agent/team/work/<n>-build.md` for the Book Cloud slice and
proposes the `book_cloud` table DDL. PM is available for the horizon-awareness
brief (#2) as soon as the cloud slice is moving.
