# AGENT-TEAM — objective owners for Oliver

Three objective owners maintain Oliver and the R/W Book Club system. Each owns a
durable outcome through measurement, implementation, verification, deployment
acceptance, and natural club acceptance. There is no dispatcher, Build Manager,
Product Manager, or Team Manager.

## The team

| Objective | File | Cadence | Primary question |
|---|---|---|---|
| **Run Oliver** | `run-oliver.md` | Weekly and after incidents/deploys | Is Oliver and the public site healthy, current, recoverable, observable, and inexpensive to run? |
| **Understand the Club** | `understand-club.md` | Every eight weeks and when the club record changes materially | Does Oliver's model of the club reflect authoritative history, member taste, culture, and current reading context? |
| **Improve Oliver** | `improve-oliver.md` | Weekly and after meaningful behavior changes | Is Oliver actually useful, grounded, restrained, and improving across Discord, email, meetings, and the site? |

Building and testing are capabilities of every objective owner. New member-facing
behavior, communication cadence, non-review club-record writes, schedule/order
changes, and other consequential product direction still belong to Jamie.

Choose the owner by the primary failed outcome, not by the file being edited:

- **Run Oliver** when execution, delivery, persistence, uptime, recovery, or cost is wrong.
- **Understand the Club** when authoritative club facts, provenance, projection, or source
  meaning is wrong.
- **Improve Oliver** when the sources are sound but Oliver's behavior, judgment, grounding,
  restraint, or usefulness is wrong.

Cross-cutting work keeps one originating owner. The other objectives contribute an acceptance
standard or capability; they do not create a second owner or a handoff queue.

The former seven-role queue and dispatcher are retired. Git history and `work/`
preserve that period. Historical design records may use the old role names; they are
evidence, not current routing instructions.

## Project map

- `AGENTS.md` points to `CLAUDE.md`, the architecture and operating source of truth.
- `agent/docs/SOUL.md`, `PURPOSE.md`, and `PROCESS.md` own Oliver's identity, purpose,
  and approved member-communication cadence.
- `club_*` SQLite is the canonical club record. `corpus/data/` is generated and
  gitignored; never hand-edit it.
- `scripts/evaluator_evidence.py` collects exact, private production evidence for
  evaluation without mutating production.
- `uv run --locked python -m agent.publish` regenerates the corpus, builds the site,
  and publishes `gh-pages`.

## Issue policy

Issues are an exception ledger for multi-run work, external blockers, and Jamie
decisions. Same-run findings are fixed and verified without a routing ticket. Every
open issue has exactly one ownership label:

| Label | Owner |
|---|---|
| `objective:run` | Run Oliver |
| `objective:club` | Understand the Club |
| `objective:agent` | Improve Oliver |

Work-type labels such as `bug`, `operations`, `culture`, `eval`, and `enhancement`
remain descriptive. They do not select a worker. `decision` means Jamie must answer
before the objective can continue.

When a change cannot complete technical or natural acceptance in the same run, the issue or the
originating automation's `Active watches` entry records the commit, originating objective,
technical state, semantic predicate, and next natural evidence window. Absence of evidence is a
pending watch, not a failed behavior.

## Cadence and activation

The RRULEs in `automations.toml` implement calendar cadence only. Phrases such as "after an
incident," "after a deploy," or "after a behavior change" are intentional manual triggers for the
current owner; the retired dispatcher is not replaced. While an activity is `PAUSED`, neither its
calendar cadence nor those event phrases cause an automatic run.

## Human and privacy boundary

- Never manufacture a Discord message, DM, email, meeting communication, corpus
  write, or site content change for acceptance.
- Oliver may communicate only through the approved cadence and authority in
  `agent/docs/PROCESS.md`; an operator or evaluator does not trigger that work early.
- Never commit member PII or publish private evaluator evidence. Production Discord
  and email evidence is read-only and summarized privately.
- Jamie authorizes schedule changes and non-review club-record writes. Members retain
  authority over their own reviews.

## North star

Oliver should feel like a grounded, useful sixth member of this particular book club.
Prefer measured outcomes over tickets, the smallest source fix over a guard, and a
healthy no-op over invented work.
