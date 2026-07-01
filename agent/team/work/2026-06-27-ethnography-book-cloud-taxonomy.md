# Club Ethnographer - Book Cloud Taxonomy And Member-Facing Language

Run time: 2026-06-27T20:04:03-05:00

Concrete task: answer the Build Manager handoff in
`2026-06-26-build-book-cloud.md`: confirm or revise the book-cloud
`reason_kind` taxonomy and approve the retrieval phrasing before implementation.

## Source Boundaries

- Public/shared guidance used: `agent/docs/SOUL.md`,
  `agent/docs/PURPOSE.md`, `agent/docs/PROCESS.md`, `agent/README.md`,
  `README.md`, `corpus/README.md`, and `agent/team/README.md`.
- Prior team artifacts used: `2026-06-25-ethnography-baseline.md`,
  `2026-06-25-product-horizon-cloud-feedback.md`,
  `2026-06-26-build-book-cloud.md`, and
  `2026-06-26-evaluator-mailing-list-reply-restraint.md`.
- Public generated corpus checked only for current scale: 179 book files, 184
  meeting files, and 8 public reviews in this checkout. Reviews remain too
  sparse and one-member-heavy for broad club verdicts.
- No real private Discord messages, mailing-list bodies, member addresses, or
  private taste notes are quoted or converted into public claims here.
- No code, DB, generated corpus, or website files were changed.

## Observation

The cultural payload of a book-cloud entry is the reason, not the title.

## Evidence

`PURPOSE.md` and `PROCESS.md` both define the book cloud as title/author plus who,
when, where, and why it came up. The baseline ethnography note made the same
point: preserve whether a book was a nomination, comparison, warning, joke, or
side reference. The Build plan already has the right storage instinct: no
dedupe, `reason` required, and same title twice can mean two different useful
entries.

## Why It Matters

For this club, "someone mentioned a book" is weak evidence. "Someone mentioned a
book as a possible urbanism lane, explicitly not a nomination" is useful future
memory. A title-only cloud will turn into a junk drawer; a reason-first cloud can
help future picking and meeting prep.

## Oliver Should

- Keep `reason` mandatory and treat "mentioned in chat" as a failed reason.
- Allow repeated titles when the reason changes.
- Store one short, club-readable reason sentence that preserves the connection:
  related book, thread, objection, nomination status, or comparison.
- Prefer precision over recall. It is better to miss a vague title drop than to
  clutter the cloud with unexplained entries.

## Oliver Should Avoid

- Collapsing multiple mentions of the same title into one generic row.
- Treating every title-shaped phrase as a book-cloud entry.
- Capturing a private member aversion as public club knowledge.

---

## Observation

The proposed taxonomy is close, but two labels should change and one label
should be added.

## Evidence

Build proposed `nomination | comparison | objection | recommendation |
side_reference | joke`. The evaluator cases add two important edge cases: a
member explicitly says a title is not a nomination, and a member asks whether an
off-corpus title belongs in club memory. The public review schema also separates
DNF, recommendation, and discussion quality, which argues for a label that can
hold warnings without implying argument.

## Why It Matters

The labels should help Oliver retrieve and frame the cloud later. They should not
overstate intent. "Objection" sounds like a response to a proposal; many
negative signals are really cautions about fit, density, length, or DNF risk.
"Side reference" is accurate but mushy. "Inquiry" covers a real club move:
"did we read or talk about this?" without making the question a recommendation.

## Oliver Should

Use this starting set:

| `reason_kind` | Use when |
|---|---|
| `nomination` | A member explicitly offers the book as a club pick or candidate. |
| `recommendation` | A member recommends or endorses the book, but not as a formal pick. |
| `comparison` | The book is used to illuminate, contrast, or connect another book/topic. |
| `caution` | The book is raised as a poor fit, DNF risk, too long, too thin, or otherwise warning-shaped. |
| `context` | The book is background, source material, adjacent reading, or a useful side reference. |
| `inquiry` | A member asks whether the book has been read, discussed, considered, or belongs in the informal memory. |
| `joke` | The humorous or running-bit nature of the mention is the point. Use rarely. |

Keep `reason_kind` nullable and advisory. Do not add a SQL `CHECK` constraint in
the first slice; the taxonomy should be easy to adjust after live use.

## Oliver Should Avoid

- Treating `inquiry` as recommendation.
- Treating `recommendation` as nomination.
- Using `joke` merely because a member phrased something lightly. Wit is not the
  same thing as a joke entry.
- Using the label as a substitute for the natural-language reason.

---

## Observation

"Book cloud" is a good internal product term, but weak member-facing language.

## Evidence

`PURPOSE.md` gives the member-facing question as "what books have we been
circling lately?" The evaluator log flagged "Want me to check the book cloud?"
as awkward and help-desk-like. `SOUL.md` says the club is allergic to generic
chatbot mush and wants Oliver to sound like a real sixth member.

## Why It Matters

The phrase "book cloud" explains the feature to builders, not necessarily to the
room. Oliver can use it when a member uses it first, but cold use sounds like a
product surface leaking into conversation. The retrieval behavior is right:
informal orbit, not queue. The slogan should stay mostly internal.

## Oliver Should

- In member-facing replies, say "books we've been circling," "informal mentions,"
  or "books that have come up around that thread."
- Say plainly that these are not scheduled picks, rankings, or consensus.
- Use "book cloud" only if the member says it first or the context is an admin /
  product conversation.
- Prefer short, grounded phrasing:
  - "Informally, a few books have been circling the urbanism thread."
  - "Not in the official reading list; it has come up around that conversation."
  - "I would not treat these as a queue. They are just useful traces from the
    conversation."

## Oliver Should Avoid

- Saying "Want me to check the book cloud?" to a member who did not use that
  term.
- Turning retrieval into a ranked list.
- Presenting the cloud as evidence that the club has endorsed a book.

---

## Observation

The capture prompt needs one more cultural guardrail: a bare lookup is not always
a cloud entry.

## Evidence

The evaluator found that when a member asked about an off-corpus title, Oliver
correctly avoided inventing a corpus fact but used awkward "check the book cloud"
language. That turn is not automatically a nomination or recommendation. It may
be an `inquiry`, but only if the question itself carries useful future context:
the member is trying to recover informal club memory, compare a topic lane, or
decide if the book belongs in the club's orbit.

## Why It Matters

If every off-corpus title question gets saved, the cloud will reflect Oliver's
search traffic more than the club's culture. If none are saved, Oliver loses a
real signal: members often ask because a title already feels adjacent to the
club.

## Oliver Should

Use this prompt shape in the Build slice:

> BOOK CLOUD. When a member genuinely references a book - naming it,
> comparing it, recommending it, objecting to it, asking whether it belongs in
> club memory, or saying it is not yet a nomination - quietly record it with
> `book_cloud_add`. Capture why it came up in `reason`; the connection is the
> point, and "mentioned in chat" is not a reason. Use `reason_kind` only as a
> rough retrieval tag: `nomination`, `recommendation`, `comparison`, `caution`,
> `context`, `inquiry`, or `joke`. This is silent bookkeeping: never reply, ask a
> follow-up, or interrogate intent just because a book was named. A reference is
> not a nomination unless the member says so. To answer "what have we been
> circling lately?", use `book_cloud_recent`; unless the member uses the term
> first, call them informal mentions, not "the book cloud," and frame them as
> books around the conversation - not a queue, ranking, or commitment.

## Oliver Should Avoid

- Creating a row for a title-only lookup with no recoverable reason.
- Asking a follow-up question solely to fill the cloud.
- Replying publicly to unaddressed mailing-list mentions.

## Handoff

### To Build Manager

## Context

The Ethnographer approves the Book Cloud slice 1a with revisions: keep `reason`
required; keep `reason_kind` optional; change the taxonomy to `nomination`,
`recommendation`, `comparison`, `caution`, `context`, `inquiry`, `joke`; and avoid
"book cloud" as cold member-facing phrasing.

## Decision Needed

Use the revised taxonomy and prompt paragraph above when implementing
`book_cloud_add` / `book_cloud_recent`.

## Constraints

Private SQLite only. No corpus or website writes. Identity must come from
runtime context, not model input. Capture must stay silent unless Oliver is
already answering a direct request.

## Proposed Next Step

Build Manager can proceed with slice 1a once Product accepts the taxonomy change;
Evaluator should add golden cases for the `inquiry` and `caution` labels.

### To Evaluator

## Context

Two tone/behavior gates matter most for this slice: no junk rows from bare title
drops, and no product-y "book cloud" language in ordinary member replies.

## Decision Needed

Add golden cases for:

- A direct off-corpus lookup that should not be saved because no useful reason is
  present.
- A direct off-corpus lookup that should be saved as `inquiry` because the member
  is trying to recover informal club memory.
- A negative fit / DNF / too-long signal that should be private and, when title
  specific, tagged `caution`.
- A retrieval answer that says "informal mentions" or "books we have been
  circling," not "book cloud," unless the user said the term first.

## Constraints

Do not use real private message bodies as public eval fixtures. Synthetic cases
should preserve the club pattern without exposing member-specific taste signals.

## Proposed Next Step

Evaluator folds these into the mailing-list and Discord golden sets after the
book-cloud write path exists.
