# Build Plan — Five-book horizon awareness, read-only (P1)

**Date:** 2026-06-26
**Role:** Build Manager
**Upstream:** `2026-06-25-product-horizon-cloud-feedback.md` (PM priority #2, "Second
Slice" sketch) and `2026-06-25-ethnography-rhythm-and-ritual.md` (thinning-horizon
→ Picking-meeting insight).
**Task (one sentence):** Turn the PM's read-only horizon-awareness slice into a
file-level implementation plan, propose the `horizon()` return contract, and draw
the one design decision the rotation rule actually forces.

> Not implemented in this run. Plan + contract proposal per the routine contract
> (plans, not edits, unless the invocation asks for code). The anchoring rule
> (below) needs a Product call before coding starts.

This is the *other* open P1 alongside the Book Cloud slice
(`2026-06-26-build-book-cloud.md`). The two are independent: this one is pure
read-only corpus computation, no SQLite, no writes, no scheduler.

---

## The two findings that reshape the slice

**Finding 1 — the rotation source already exists; only the forward walk is
missing.** `agent/club/meeting_rules.py:47` already has:

```python
def _current_members() -> list[dict]:
    return sorted([m for m in corpus_read.members() if m.get("isCurrent")],
                  key=lambda m: m.get("name") or m.get("slug") or "")
```

Sorting current members by first name *is* the documented rotation — it returns
`Erik, Jamie, Loren, Nick, Tom` today, exactly PROCESS.md's order, and it
self-heals when membership changes (no hardcoded name list to rot). `next_meeting()`
(same file, line 54) already resolves the next scheduled meeting + its picker. So
the slice is small: walk the existing rotation forward from the existing anchor.
This also argues the computation's **home is `meeting_rules.py`, not
`corpus_read.py`** as the PM sketched — the rotation is a *club rule* (deterministic
by first name, current-members-only), and `corpus_read.py` is deliberately pure
corpus joins with no rule semantics. Noted deviation from the PM sketch, reason
given; flagged for review, not silently taken.

**Finding 2 (load-bearing) — the deterministic rotation does not match reality.**
Verified against the live corpus today, the last several *actual* pickers were:

| Meeting | Picker |
|---|---|
| 2025-08 The Origins of Totalitarianism | Loren |
| 2025-09 Heart of Darkness | Nick |
| 2025-11 Enshittification | Tom |
| 2026-01 The Overstory | Erik |
| 2026-05 Patterns in Nature | **Tom** |
| 2026-06 A World Appears *(upcoming, only scheduled meeting)* | Jamie |

Tom picked again (2026-05) out of strict alphabetical turn. So a naive walk —
"last scheduled picker is Jamie ⇒ next four are Loren, Nick, Tom, Erik" — will
sometimes name a member the club's lived rotation would not consider "up." The
horizon's headline answer ("whose pick is missing?") is exactly this number, so
the anchoring rule is not cosmetic. **This is the Build→Product decision below.**

**Live reality that makes this urgent:** the horizon is **one meeting deep**.
`upcoming_meetings()` returns a single future placeholder (A World Appears,
2026-06-30). After June 30 there is nothing scheduled. Whatever anchoring rule we
pick, the feature's first real output is "4 of the next 5 slots are empty" — the
leading indicator the ethnographer's rhythm note asked Oliver to be able to *see*.

---

## Goal

Make Oliver able to *answer* — read-only, no nudging — "what are the next five
books?", "whose pick is missing?", and "how thin is the horizon?", grounded in the
deterministic host rotation and the scheduled meetings in the corpus. Awareness
only; the nudge/cadence/pick-in-channel behaviors are the separate P2 slice (PM #4).

## Files Likely Touched

| File | Change | Notes |
|---|---|---|
| `agent/club/meeting_rules.py` | Add `horizon(depth: int = 5) -> dict` (+ a small `_rotation_from(anchor_slug)` helper) | Reuses `_current_members()` and `next_meeting()` already here. No new corpus reads beyond `members()` / `upcoming_meetings()`. |
| `agent/tools.py` | Append a `horizon` tool schema to `TOOLS`; add one `dispatch()` branch `if name == "horizon": return _dump(meeting_rules.horizon(...))` | Append to the **end** of `TOOLS` to keep the cached tool-prefix stable (file's stated invariant, `tools.py:33`). `meeting_rules` is already imported (`tools.py:29`). |
| `agent/oliver.py` | One sentence in `OPERATIONAL_PROMPT`: when asked what's next / whose pick is missing, call `horizon`; frame an empty slot as the club's own runway, never as pressure | Awareness framing only; no nudge authority. |
| `agent/context.py` | *Optional, recommended:* extend the existing `Upcoming:` line in `club_context()` to note the first empty rotation slot when the horizon is thin | At-a-glance grounding so Oliver knows the horizon is thin without a tool call. Cheap; keep it one short clause. |
| `tests/test_meeting_rules.py` | Unit tests for `horizon()` (full/thin/empty/wrap-around, depth clamp) against a synthetic corpus | Matches existing `meeting_rules` test style. |
| `tests/test_tools_dispatch.py` | `horizon` dispatch returns a JSON list/obj; depth clamps | Matches existing dispatch happy-path style. |
| `agent/README.md` | One line under Tools listing `horizon` | Docs-update-when-behavior-changes. |

No `website/`, no `corpus/data/`, no `gitwrite.py`, no `db.py`, no `scheduler.py`.
Pure read-only.

## Proposed `horizon()` return contract (sign-off needed before coding)

```python
def horizon(depth: int = 5) -> dict:
    """Read-only view of the next `depth` host slots in the deterministic
    first-name rotation, each paired with its scheduled book or marked empty."""
    return {
        "rotation": ["Erik", "Jamie", "Loren", "Nick", "Tom"],   # current members, by first name
        "anchorMeeting": {                                       # last scheduled meeting we walk forward from
            "date": "2026-06-30", "book": "A World Appears", "picker": "Jamie",
        },
        "slots": [
            {"position": 1, "picker": "Loren", "pickerSlug": "loren",
             "book": None, "meetingDate": None, "status": "empty"},
            # ... one row per upcoming slot ...
        ],
        "scheduledCount": 1,        # slots with a real scheduled book
        "emptyCount": 4,            # slots still needing a pick
        "firstEmptyPicker": "Loren",# the headline "whose pick is missing" answer (anchor-rule dependent)
        "depth": 5,
    }
```

Design decisions baked in (called out so review is cheap):

- **Rotation = current members sorted by first name** (reuse `_current_members()`),
  *not* a hardcoded `["Erik", ...]` list and *not* derived from `club_meeting_hosts`
  history. Self-heals on membership change; matches PROCESS.md by construction.
- **Anchor = the last *scheduled* meeting** (`next_meeting()`), per the PM sketch —
  not the last *read* meeting. With one upcoming meeting that anchor is Jamie/Jun 30.
- **Placeholder dates are soft.** A slot with a placeholder meeting still counts as
  `scheduled` (the book exists); only slots with *no* book are `empty`. Date softness
  is surfaced, never used to drop a slot.
- **No writes, no nudge fields.** `status` is just `scheduled` | `empty`. No
  "should nudge", no cadence — that's the P2 slice and would be a category error here.
- **`depth` clamped to `[1, 8]`** in dispatch, like the other read tools.

## THE decision needed before coding (Build → Product)

**When the scheduled pick history diverges from the strict alphabetical rotation
(it does — Tom, 2026-05), what determines "whose pick is next / missing"?**

- **(a) Strict deterministic walk** from the last scheduled picker's position in
  the cycle (PM sketch's literal wording). Simple, fully reproducible, but will
  sometimes name a member the club wouldn't consider "up" given recent reality
  (e.g. naming someone who just picked).
- **(b) Fairness/recency walk** — next picker = the current member who has gone
  longest without a *scheduled* pick (least-recently-scheduled first). Tracks the
  club's actual fairness instinct; slightly more logic; needs the per-member
  last-scheduled date (already derivable from `books()`).
- **(c) Hybrid** — start from (a)'s deterministic order but skip any member who
  already holds an upcoming scheduled slot, so the same person isn't double-counted.

**Build recommends (b)** — it is what "whose pick is missing" *means* to the club
and it degrades gracefully when reality is messy, which it demonstrably is. (a) is
cheapest and matches the doc literally; (c) is a reasonable middle. This is a
product-semantics call, not an engineering one — hence the handoff. The contract
above is identical under all three; only `firstEmptyPicker` and the `slots[].picker`
assignment change.

## Implementation Steps

1. **meeting_rules.py** — add `_rotation_from(anchor_slug)` (cycle the
   `_current_members()` order starting after the anchor's picker) and `horizon(depth)`.
   Build the chosen anchoring rule (a/b/c) once the Product call lands; the
   surrounding contract is fixed.
2. **tools.py** — append the `horizon` schema (`{depth?}`) and one dispatch branch;
   clamp `depth` to `[1, 8]`.
3. **oliver.py** — one `OPERATIONAL_PROMPT` sentence (awareness, not pressure).
4. **context.py** *(optional)* — append the first-empty-slot clause to the
   `Upcoming:` line when `emptyCount > 0`.
5. **Tests + one README line** — per below.

## Tests / Evals

Deterministic unit tests (no model calls; keep the suite fast):

- `test_meeting_rules.py`
  - **Full horizon:** 5 scheduled future meetings, one per member ⇒ `emptyCount == 0`,
    `firstEmptyPicker is None`, slots in rotation order.
  - **Thin horizon (the live case):** one scheduled meeting (Jamie) ⇒
    `scheduledCount == 1`, `emptyCount == 4`, `firstEmptyPicker` == whatever the
    chosen rule yields (assert the rule, lock it in).
  - **Empty horizon:** zero upcoming ⇒ all `depth` slots empty, no crash.
  - **Wrap-around:** anchor is Tom ⇒ next slot is Erik (cycle wraps).
  - **Membership change:** drop a current member ⇒ rotation length follows
    `_current_members()`, no hardcoded `5`.
  - **Depth clamp:** `depth=0` and `depth=99` clamp into `[1, 8]`.
- `test_tools_dispatch.py`
  - `horizon` returns a dict with `slots`; `depth` out of range is clamped.

Behavioral evals — **Evaluator owns**, flagged not skipped:

- Grounding/voice case: "what are we reading after A World Appears?" judged on
  (1) correctly saying the horizon is *thin* / mostly empty, (2) naming the right
  first-empty picker, (3) framing it as the club's runway, **not** a nudge or a
  demand (SOUL: awareness without pressure; ethnographer's "preserve meeting time
  for books, not procedure"). This is the acceptance gate for the slice.

## State / Migration Notes

- None. Pure function over the corpus; no table, no migration, no backfill.
- Adding one tool invalidates the cached tool-prefix exactly once on deploy
  (tools render before the system prompt) — expected, one cache-miss turn.

## Rollout Notes

- Purely additive and read-only: no scheduler change, no external action
  (no email/DM/post), no member-visible side effect until a member asks.
- No flag needed — there's no destructive path. If the anchoring rule reads wrong
  in practice, the fix is prompt- or single-function-local; no data to clean up.
- Rollback = revert the touched files; nothing persisted.

## Risks

- **Anchoring rule mismatch (the real one).** Whatever rule ships, it can name a
  "next picker" the club wouldn't, because recent history breaks the documented
  rotation. *Mitigation:* get the Product call first; the Evaluator grounding case
  asserts the rule; the framing stays "here's the runway," never "X, you're up,"
  so a wrong name is a soft miss, not a false summons.
- **Placeholder-date softness leaking into certainty.** The single upcoming
  meeting is a placeholder; Oliver must not assert Jun 30 as fixed. *Mitigation:*
  carry `placeholder`/date-soft through the contract; the prompt already tells
  Oliver to verify dates (`current_meeting_status` exists for exactly this).
- **Scope creep toward nudging.** A horizon this thin (4 empty) will tempt a
  "should I nudge Loren?" instinct mid-build. *Mitigation:* `status` has no nudge
  field by design; nudging is the gated P2 slice and stays out.

---

## Handoffs

### To Product Manager (blocking — the anchoring rule)

**Context:** The deterministic first-name rotation diverges from the club's actual
scheduled-pick history (Tom picked out of turn, 2026-05). "Whose pick is missing?"
resolves differently under a strict-deterministic walk vs. a recency/fairness walk.
**Decision Needed:** Pick the anchoring rule — (a) strict deterministic, (b)
least-recently-scheduled, or (c) deterministic-minus-already-scheduled. Build
recommends (b). The return contract is unchanged across all three.
**Constraints:** Read-only awareness slice; no nudging; current-members-only
rotation; placeholder dates are soft.
**Proposed next step:** PM picks the rule (one line is enough); Build implements
`horizon()` and the locked-in unit test for `firstEmptyPicker`.

### To Evaluator (parallel — owns the grounding/voice gate)

**Decision Needed:** A grounding case for "what's next after A World Appears?"
judged on thin-horizon honesty, correct first-empty picker, and awareness-not-
pressure framing.
**Constraints:** Read-only; no corpus/website assertion; no nudge language.
**Proposed next step:** Stand the case up against `horizon()`'s output once the
anchoring rule is set; reuse it later as the gate for the P2 nudge slice.

### To Club Ethnographer (light — framing only, non-blocking)

**Context:** The rhythm note already asked that a thinning horizon read as a
leading indicator and a *Picking-meeting* opportunity, not a chore.
**Decision Needed:** Confirm the awareness-line framing ("the club's runway" /
"a couple of open slots after June") matches the club's voice before the P2
offer-to-pick behavior rides on it.
**Proposed next step:** A one-paragraph framing note; not blocking 1a-equivalent
landing, since this slice only *answers*, it does not offer.
