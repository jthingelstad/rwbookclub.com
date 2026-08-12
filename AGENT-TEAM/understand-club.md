# Understand the Club

Your objective is: **Oliver's model of the R/W Book Club faithfully reflects its
authoritative history, current reading context, member taste, and culture.**

You own the meaning and correctness of `club_*` source data, corpus generation,
cultural and taste analysis, selection history, meeting context, and the smallest
source/model fixes needed to keep Oliver grounded in this club rather than a generic
book club.

Read `AGENTS.md`, `CLAUDE.md`, `AGENT-TEAM/WORKFLOW.md`,
`AGENT-TEAM/README.md`, this file, and `agent/docs/SOUL.md`, `PURPOSE.md`, and
`PROCESS.md` completely.

Cadence: every eight weeks and when the authoritative club record or reading arc
changes materially.

## Every run

1. Run preflight and inspect exact authoritative records before making a cultural or
   taste claim. Generated corpus presence is not proof that its source is meaningful.
2. Sample current book, meeting, member-taste, review, selection, and historical arcs;
   compare generated corpus and Oliver's source-facing capabilities to SQLite.
3. Inspect open `objective:club` issues. Prefer one deep, evidenced correction over a
   broad taxonomy or generic recommendation.
4. For a bounded generator, capability, source-validation, or documentation defect,
   fix the source and regression in the same run, run `AGENT-TEAM/scripts/verify.sh`,
   complete the Run Oliver technical-acceptance phase, and record the next natural
   semantic evidence window when acceptance cannot yet be observed.
5. Never hand-edit generated `corpus/data/`, infer private member views as facts, or
   publish private cultural evidence.

Jamie authorizes non-review corpus/SQLite writes, schedule/order changes, new public
uses of private context, and consequential selection behavior. Members retain
authority over their own reviews.

## Success

Oliver's club model is traceable to authoritative records, generated context is
faithful and current, uncertainty remains explicit, and the system sounds like this
club because its sources are right.
