# Evaluator findings — Oliver voice/compose layer

**Date:** 2026-06-25
**Role:** Evaluator
**Behavior under evaluation:** the recently-added voicing layer — `oliver.compose()`
(voices proactive/templated surfaces in Oliver's register, degrades to a literal
template on LLM failure), its callsites in `agent/commands.py`
(roll-call, reading check-ins, scheduler notifications, command acks), the
`scheduler.due_notifications` facts/fallback pairs, and `mail/signature.py`'s
contextual email signature.

**Risk being tested:** proactive copy is now LLM-generated at send time. The
load-bearing claim (in `compose`'s docstring) is that *"the facts are
authoritative — Oliver only voices them … which keeps counts and dates correct."*
That claim is currently asserted in prose and enforced only by a prompt
instruction. Nothing verifies the model actually preserves the facts, stays in
voice, or respects the email envelope. The club will see this output unsupervised.

**Test status:** `test_compose.py`, `test_persona.py`, `test_signature.py`,
`test_scheduler.py`, `test_meeting_campaign.py`, `test_meeting_rules.py`,
`test_oliver_mailing_list.py` — all green (32 passed). The findings below are
about *coverage gaps and one latent defect*, not red tests.

---

## Summary of findings

| # | Severity | Finding |
|---|----------|---------|
| F1 | High | `compose()` fact-fidelity + voice is unverified — only mechanical unit tests exist. The central correctness claim has no behavioral eval. |
| F2 | Medium-High | Double-signature risk: voiced email path can emit its own "— Oliver" *before* the auto-appended `email_signature`, producing two sign-offs. Asserted by prompt only, no test. |
| F3 | Medium | The behavioral eval harness (`tests/eval.py`) covers only reactive Discord Q&A. None of the proactive surfaces, mailing-list silence judgment, DNF handling, or private-feedback privacy are in any golden set — though the role lists them as must-test. |
| F4 | Medium | The reading-check-in *attendance gate* (`snapshot()` only feeds `status == "yes"` members into `needsReading`) is untested. Timing/cap tests all pre-build `needsReading`, so the "don't ask un-confirmed members for reading progress" rule has no regression guard. |
| F5 | Low | `compose()` reuses the full interactive `OPERATIONAL_PROMPT` (tool strategy, "after any tool calls always compose a reply", web-search rules) for a tool-less single shot. Mostly inert, but it invites off-corpus hedging on facts that are authoritative by construction. |

---

## F1 — `compose()` voicing is unverified beyond plumbing

### Finding
`test_compose.py` checks four mechanical things: returns model text, falls back
on exception, falls back on empty completion, and that the *email prompt string*
contains "sign off". It never feeds `compose()`'s real prompt to a model and
checks the **output**. The three properties that actually matter are untested:

1. **Fact fidelity** — every name, count, and date in `facts` survives into the
   prose, and nothing is invented or dropped. This is the explicit design claim.
2. **Voice** — no "As an AI assistant", no help-desk sign-off, no markdown
   headings, no bulleted lists in the `discord` medium.
3. **Envelope** — `discord` has no greeting/sign-off; `email` opens with a
   greeting and does *not* sign off (see F2).

### Why it matters
This is the Evaluator anti-pattern made literal: *"passing evals that only check
for plausible text."* The whole point of `compose()` over a static template is
that it sounds like Oliver while staying exact. If it silently rounds "2026-07-28"
to "late July" or drops the picker, members lose trust in proactive messages
specifically — the ones they can't sanity-check against a question they asked.

### Reproduction / Scenario
Roll-call announcement with `meeting date: 2026-07-28`, `quorum rule: we need 3
of 5 current members, and the picker has to attend`. A weak voicing might say
"sometime late this month" or drop the picker clause.

### Expected Oliver behavior
The composed message contains the exact date and preserves the quorum/picker
facts, in one or two warm sentences, no headings, no sign-off.

### Actual behavior
Unknown — there is no eval that exercises it. Untested by construction.

### Suggested fix
Add an LLM-judge eval for `compose()` modeled on `tests/eval.py`'s judge, run
over the real callsite fact-dicts. A golden set of ~6 fact-dicts (roll-call
announce, roll-call reminder, roll-call email, reading-checkin email, schedule
ack, review ack, milestone, anniversary), each judged on:

```
- fact_fidelity: every name/number/date in `facts` appears; nothing invented or dropped (1–5)
- voice:        warm, in-character, no assistant boilerplate, no markdown headings (1–5)
- envelope:     discord → no greeting/sign-off; email → greeting present, no sign-off (1–5)
- brevity:      ≤ ~3 sentences / fits one Discord message (1–5)
```

Gate CI on `fact_fidelity == 5` for the deterministic facts (dates, counts,
slugs). Keep it cheap: it's a single tool-less call per case.

---

## F2 — Double sign-off in the voiced email path

### Finding
Both email callsites append the signature unconditionally:

```python
# agent/commands.py:257 and :311
body = await asyncio.to_thread(oliver.compose, ..., medium="email")
body = body.rstrip() + "\n\n" + await asyncio.to_thread(signature.email_signature)
```

`email_signature()` begins with `"— Oliver"`. The `compose(medium="email")`
prompt says *"Do not sign off — a signature is added automatically,"* but that is
an instruction, not a guarantee. Models sign emails reflexively. When the voiced
body ends with its own "— Oliver" / "Thanks, Oliver", the member receives:

```
…can you make the meeting?

— Oliver

— Oliver
📚 Next up: Stiff, picked by Tom on 2026-07-28.
We've been meeting for 23 years and counting.
```

The **fallback** templates (`_roll_call_email_body`, `_reading_checkin_body`) end
on a status/instruction line with no sign-off, so the template path is clean. The
defect is **asymmetric**: it only appears on the voiced path, which is the default.

### Why it matters
A doubled signature is the exact "AI wrote this" tell the SOUL warns against
("do not sign off like a service desk"). It's small, but it's the kind of seam
that makes a technical club trust the automation less — and it's invisible to the
unit tests, which only inspect the prompt.

### Reproduction / Scenario
Any roll-call or reading-checkin email where the model appends a sign-off.

### Expected Oliver behavior
Exactly one sign-off (the appended contextual signature).

### Suggested fix
Two options, prefer the defensive one:
1. **Strip a trailing sign-off from the composed body before appending the
   signature** — a small `_strip_signoff(text)` that removes a trailing
   `—/-/–\s*Oliver` (and "Thanks, Oliver" / "Best, Oliver") line. Belt-and-
   suspenders, independent of model compliance.
2. Add a regression test that feeds a sign-off-containing completion through the
   email callsite and asserts the final body has exactly one "Oliver" sign-off
   block.

Proposed regression (drop into `tests/test_signature.py` or a new
`test_email_compose_envelope.py`):

```python
def test_voiced_email_does_not_double_sign(monkeypatch):
    # Model reflexively signs off; the assembled email must still end with
    # exactly one signature block.
    monkeypatch.setattr(oliver, "compose",
                        lambda *a, **k: "Hi Tom, can you make it? — Oliver")
    body = assemble_roll_call_email(...)        # the compose + signature concat
    assert body.count("— Oliver") == 1
```

---

## F3 — Behavioral eval harness has not grown to the new surfaces

### Finding
`tests/eval.py`'s golden set (`GOLDEN_SINGLE`, `GOLDEN_MULTI`) is entirely
reactive Discord Q&A: identity, memory, grounding, past-picks. The role's
Must-Test Scenarios list ten behaviors; the harness covers ~3. Absent:

- **Mailing-list silence judgment.** `test_oliver_mailing_list.py` tests the
  *sentinel plumbing* (parsing `[[NO_REPLY: …]]`) with `answer()` mocked. The
  actual *decision* — stay silent on a bare mention / a question aimed at the
  humans, but answer when addressed by name — is never run against a model. The
  anti-pattern "ignoring silence as a behavior" applies directly.
- **Five-book horizon nudge targeting** — does Oliver nudge the *right* host in
  the rotation, via DM/email not the shared channel.
- **Reading-progress gating** — ask only after a "yes" (see F4).
- **DNF as a strong negative signal** — a member saying "I didn't finish it"
  should be treated as meaningful selection feedback, not a metadata shrug.
- **Private feedback stays private** — a taste signal/concern must not become
  website review copy.
- **Two-day topic email** — provocations that fold in *prior club books*, not a
  generic agenda for the current one.

### Why it matters
The bulk of new code (compose, meeting campaign, scheduler, email) is exactly the
member-visible, harder-to-eyeball surface, and it has the least behavioral
coverage. One reactive golden set "standing in for the range of member styles"
is the listed anti-pattern.

### Suggested fix
Extend `eval.py` (or add `eval_proactive.py`) with two new case types:
1. **Silence cases** — feed `answer_mailing_list_email` real list emails (named
   vs. unnamed mention, question-to-humans vs. question-to-Oliver) and judge the
   reply/silence decision, not just sentinel parsing.
2. **Proactive-copy cases** — the F1 compose judge, plus a "two-day topic email"
   case judged on whether it cites at least one *prior corpus book* by name with
   a real connection (grounded, via `generate()` which has tools).

---

## F4 — The reading-check-in attendance gate is untested

### Finding
`meeting_campaign.snapshot()` is the only place the "yes" filter lives:

```python
elif row["status"] == "yes":
    if reading_ok: ...
    else:
        combined["nextAction"] = "reading_checkin"
        needs_reading.append(combined)     # ← only "yes" members reach here
```

`reading_checkin_candidates()` then draws solely from `needsReading`. But every
test in `test_meeting_campaign.py` *pre-constructs* `needsReading` with a "yes"
member, so the gate itself — "a `pending`/`unsure`/`no` member is never asked for
reading progress" — has no regression guard. PROCESS.md makes this a hard rule
("ask only after a member has confirmed they are attending"), and it's a listed
must-test scenario.

### Why it matters
If a refactor ever moved the reading-status check above the attendance branch (or
widened the filter), Oliver would start emailing reading check-ins to people who
never said they're coming — annoying, and a direct PROCESS violation. The timing
tests would stay green the whole time.

### Suggested fix
Add a `snapshot()`-level test (with `meeting_status` and `db.*` stubbed) asserting
that a member with `attendance == "pending"` and unknown reading does **not**
appear in `needsReading` / `reading_checkin_candidates`, and a "yes" member does.
This pins the gate where it actually lives.

---

## F5 — `compose()` inherits the tool-loop operational prompt

### Finding
`compose()` calls `_system_blocks()`, so its system prompt includes the full
`OPERATIONAL_PROMPT`: tool strategy, web-search rules, "after any tool calls,
always compose a reply — never end your turn with only tool calls," off-corpus
markers, the 1500-char Discord cap. `compose()` passes **no tools** and is a
single shot, so most of this is inert — but the off-corpus-marker rule actively
mismatches: the facts handed to `compose()` are authoritative club facts, yet the
prompt trains Oliver to hedge unmarked club specifics or note he "can't look
things up."

### Why it matters
Low severity (the facts-are-exact instruction in the compose prompt mostly wins),
but it's a latent voice risk: a roll-call could acquire an unnecessary "from what
I have on hand…" hedge that reads oddly for a message Oliver is *initiating*.

### Suggested fix
Consider a trimmed system prompt for `compose()` — CHARTER + a short "you are
voicing these exact authoritative facts; do not hedge them as uncertain" note —
instead of the full interactive `OPERATIONAL_PROMPT`. Verify via the F1 voice
axis. Not urgent; bundle with F1.

---

## Recommended acceptance tests for the voice layer (handoff to Build Manager)

1. **compose fidelity gate** (F1): LLM-judge over the real callsite fact-dicts;
   CI-gate `fact_fidelity == 5` on dates/counts/slugs.
2. **single sign-off** (F2): voiced email + appended signature ⇒ exactly one
   sign-off block; add `_strip_signoff` defense.
3. **reading-checkin attendance gate** (F4): `snapshot()` excludes non-"yes"
   members from `needsReading`.
4. **mailing-list silence judgment** (F3): real list emails through
   `answer_mailing_list_email`, judged on the reply/silence decision.
5. **two-day topic email grounding** (F3): output cites ≥1 prior corpus book with
   a real connection.

## Handoff

**To:** Build Manager (F2, F4 are concrete and ready to implement) and
Product Manager (F3's DNF / private-feedback scenarios may need a product call on
where private qualitative feedback is stored vs. the public review path).

**Decision needed:** Is F2 fixed defensively (`_strip_signoff`) or by trusting the
prompt? Evaluator recommends defensive — model compliance on "don't sign off" is
not something to ship unguarded on member email.

**Proposed next step:** Land the F2 strip + test and the F4 gate test first (small,
deterministic, no model calls). Stand up the F1 compose-judge eval as the next
slice; it unblocks F3 and F5.
