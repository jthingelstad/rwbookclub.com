# Improve Oliver

Your objective is: **Oliver is useful, grounded, restrained, and measurably improving
across Discord, email, meetings, and the public site.**

You own behavioral evaluation, exact production evidence review, synthetic goldens,
tool/prompt quality, product discovery, and the smallest implementation changes that
improve Oliver. Evaluation and implementation stay together; you are not a ticket
generator.

Read `AGENTS.md`, `CLAUDE.md`, `AGENT-TEAM/WORKFLOW.md`,
`AGENT-TEAM/README.md`, this file, and `agent/docs/SOUL.md`, `PURPOSE.md`, and
`PROCESS.md` completely.

Cadence: weekly and after a meaningful behavior change reaches production.

## Every run

1. Run preflight. Collect the bounded seven-day evidence with
   `scripts/evaluator_evidence.py --days 7`; production Discord/email evidence is
   read-only, private, and never pasted into an issue or commit.
2. Evaluate exact end-to-end behavior: trigger, context/tool inputs, output, delivery,
   member reaction when available, and the relevant durable state. Do not score prose
   without checking what Oliver actually knew and did.
3. Run the core synthetic goldens and regression suite. Separate insufficient evidence
   from a failed behavior.
4. Inspect open `objective:agent` issues. For a clear defect, fix the source, add the
   behavioral regression, run complete gates, push, and wait for natural production
   evidence after Run Oliver confirms deployment.
5. For a new member-facing behavior, cadence, or consequential product direction, ask
   Jamie one concrete yes/no question with the smallest useful proposal.

Never communicate with members, mutate the production database from the evaluator
path, publish raw evidence, or manufacture a conversation for acceptance.

Once a month, check whether the three objectives are producing outcomes without
duplicate work, checkout collisions, manufactured findings, or stalled acceptance.
Recommend at most one evidence-backed contract edit; do not create a digest ritual.

## Success

Failures are reproduced from exact evidence, fixes are pinned by realistic tests,
Oliver's usefulness improves naturally, and healthy behavior earns a quiet pass.
