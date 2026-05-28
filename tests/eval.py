"""End-to-end evaluation of Oliver: generate test questions via Sonnet, run them
through the live agent loop with tool-call tracing, judge the results via Sonnet,
and append a round to `oliver-test-log.md`.

    python -m tests.eval --round 1 --note "baseline"

Uses a per-process scratch SQLite DB so the live oliver.db isn't touched.
Multi-turn conversations use a single channel_id across turns so the rolling
summary + per-channel history exercise context retention.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import time
import uuid
from datetime import datetime, timezone

# Use a scratch DB — set BEFORE importing any agent module so db.py picks it up.
SCRATCH_DB = "/tmp/oliver-eval.db"
os.environ["OLIVER_DB_PATH"] = SCRATCH_DB
for ext in ("", "-wal", "-shm"):
    p = pathlib.Path(SCRATCH_DB + ext)
    if p.exists():
        p.unlink()

import anthropic  # noqa: E402

from agent import oliver as oliver_mod  # noqa: E402

CLIENT = anthropic.Anthropic()
MODEL = "claude-sonnet-4-6"
LOG_PATH = pathlib.Path("oliver-test-log.md")


# ── Tool-call tracing ────────────────────────────────────────────────────────
@contextlib.contextmanager
def trace_dispatch():
    """Patch agent.oliver.dispatch to capture every tool call inside the block."""
    captured: list[dict] = []
    orig = oliver_mod.dispatch

    def wrapped(name, tool_input, ctx):
        out = orig(name, tool_input, ctx)
        # Capture nearly the full output — judges need to verify Oliver's claims
        # against what the tool actually returned. Cap only to keep one runaway
        # member_history from blowing up the log file.
        snippet = out if len(out) < 8000 else out[:8000] + "…[truncated]"
        captured.append({"tool": name, "input": tool_input, "output_snippet": snippet})
        return out

    oliver_mod.dispatch = wrapped
    try:
        yield captured
    finally:
        oliver_mod.dispatch = orig


# ── JSON parsing tolerant of code fences ─────────────────────────────────────
def _parse_json(text: str) -> dict | list:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    # Last-ditch: pull the outermost { … } or [ … ].
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        for open_c, close_c in (("{", "}"), ("[", "]")):
            i, j = text.find(open_c), text.rfind(close_c)
            if i != -1 and j > i:
                return json.loads(text[i : j + 1])
        raise


# ── Question generation ──────────────────────────────────────────────────────
QGEN_SYSTEM = (
    "You generate test questions for Oliver, the R/W Book Club's Discord agent. "
    "The club has met monthly since April 2003 in Minneapolis–Saint Paul, has read "
    "179 books (~88% non-fiction), and has 5 current members: Jamie, Erik, Tom, Nick, "
    "Loren. Questions should be terse and natural — how a member actually types in "
    "chat. Not survey questions, not 'test cases that sound like tests.'"
)

QGEN_USER = """Generate {n_single} single-turn questions and {n_multi} multi-turn conversations (3–4 turns each) for test round {round_num}. Vary speakers across the 5 current members.

Cover the categories (each represented at least once across the single-turns):
- recommendations — what should I read after X / what would person Y like
- past_picks — when did we read X / what did we think of Y
- author — tell me about author X / anything else by Y
- logistics — what's next / when's our meeting / has anyone scheduled
- stats — how many books, what's the most-read topic, etc.
- opinions — member preferences / discussion follow-ups
- edge_case — ambiguous wording, misspelling, out-of-corpus, multi-step

For multi-turns, each turn should naturally build on prior context (we're testing whether Oliver tracks the conversation, not just answers in isolation).

Return ONLY valid JSON, no commentary:
{{
  "single": [{{"category": "...", "question": "...", "speaker": "Jamie|Erik|Tom|Nick|Loren"}}, ...],
  "multi":  [{{"category": "...", "speaker": "...", "turns": ["...", "...", ...]}}, ...]
}}"""


def generate_questions(round_num: int, n_single: int, n_multi: int) -> dict:
    msg = CLIENT.messages.create(
        model=MODEL,
        max_tokens=3000,
        system=QGEN_SYSTEM,
        messages=[{"role": "user", "content": QGEN_USER.format(
            n_single=n_single, n_multi=n_multi, round_num=round_num)}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    return _parse_json(text)


# ── Judging ──────────────────────────────────────────────────────────────────
JUDGE_SYSTEM = (
    "You evaluate Oliver, the R/W Book Club's AI agent, on individual interactions. "
    "Be honest and rigorous — do not award points for trying. Flag any hallucination, "
    "wrong tool, missed lookup, or off-tone reply.\n\n"
    "Oliver's tools: find_books (scored multi-angle relevance search — preferred for vague "
    "exploratory queries), search_books (precise filter browse), get_book, member_history, "
    "upcoming_meetings, club_stats, pending_reviews, get_author, club_awards, remember, "
    "recall, set_reminder, web_search (Anthropic server-side — for off-corpus world facts "
    "the model would otherwise guess at; use sparingly).\n\n"
    "Corpus: 179 books, 184 meetings, 5 current members (Jamie, Erik, Tom, Nick, "
    "Loren) + 7 former, 1 award (2016 Book of the Year: American Nations), ~10 reviews "
    "(grows over time). Topic distribution skews History & Economics, Politics & Social "
    "Sciences, Science Fiction & Fiction (the de-facto fiction bucket — ~12% of reads), "
    "Brain & Psychology, Science and Math, Technology.\n\n"
    "Oliver also CARRIES A CACHED SYSTEM CONTEXT he can speak from without a tool call: "
    "the club has met monthly since **April 2003** in the **Minneapolis–Saint Paul** "
    "area, reads ~8 books/year (88% non-fiction), and members rotate picking. The "
    "founding month, geography, cadence, member roster, and top-line stats are in this "
    "cached context — do NOT flag these as hallucinations if Oliver uses them.\n\n"
    "Oliver should ground CLUB facts (specific books, reviews, picker assignments, "
    "meeting dates) in tool output. For WORLD facts (an author's wider bibliography, "
    "public history) he may speak from general knowledge but must lead with an explicit "
    "off-corpus marker (\"outside our reading list…\" / \"not in our corpus, but…\"). "
    "Persona: warm, opinionated, brief (≤3 sentences usually), no markdown headings, "
    "no help-desk tone, no sign-offs. Italics around book titles in Discord are fine."
)

JUDGE_USER = """Evaluate this interaction.

Speaker: {speaker}
Question: {question}
{prior_block}
Tool calls (in order):
{tools_block}

Response:
{response}

Rate 1–5 (5 = optimal):
- tool_choice: right tool(s), right inputs, no missing/extra lookups
- accuracy: claims grounded in tool output; no hallucination; admits unknowns
- relevance: actually answers the question asked
- tone: in-voice for a club member; natural, brief, not help-desk-y{context_axis}

List CRITICAL ISSUES — anything that scored ≤3 on any axis, any factual error, any wrong/missing tool call. Be specific.

Return ONLY valid JSON:
{{"tool_choice": int, "accuracy": int, "relevance": int, "tone": int{context_field}, "critical_issues": [strings], "notes": "1–2 sentence assessment"}}"""


def judge_interaction(question, speaker, tools, reply, prior_turns=None):
    if prior_turns:
        prior_lines = "\nPrior turns in this conversation:\n"
        for i, t in enumerate(prior_turns, 1):
            prior_lines += f"  T{i}: {t['question']}\n     → {t['reply'][:200]}\n"
        prior_block = prior_lines
        context_axis = "\n- context_retention: tracked prior turns and responded coherently?"
        context_field = ', "context_retention": int'
    else:
        prior_block = ""
        context_axis = ""
        context_field = ""
    tools_block = (
        "\n".join(
            f"  {i+1}. {t['tool']}({json.dumps(t['input'], ensure_ascii=False)}) →\n"
            f"     {t['output_snippet'][:8000]}"
            for i, t in enumerate(tools)
        )
        or "  (none)"
    )
    msg = CLIENT.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=JUDGE_SYSTEM,
        messages=[{"role": "user", "content": JUDGE_USER.format(
            speaker=speaker, question=question, prior_block=prior_block,
            tools_block=tools_block, response=reply,
            context_axis=context_axis, context_field=context_field)}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    return _parse_json(text)


# ── Running interactions ─────────────────────────────────────────────────────
def run_single(q: dict, channel_id: str) -> dict:
    with trace_dispatch() as tools:
        reply = oliver_mod.answer(q["question"], channel_id=channel_id, speaker=q["speaker"])
    return {**q, "tools": tools, "reply": reply}


def run_multi(conv: dict, channel_id: str) -> list[dict]:
    out = []
    for turn in conv["turns"]:
        with trace_dispatch() as tools:
            reply = oliver_mod.answer(turn, channel_id=channel_id, speaker=conv["speaker"])
        out.append({"question": turn, "speaker": conv["speaker"],
                    "tools": tools, "reply": reply})
    return out


# ── Logging helpers ──────────────────────────────────────────────────────────
def fmt_tools(tools):
    if not tools:
        return "_(no tool calls)_"
    return "\n".join(
        f"- `{t['tool']}({json.dumps(t['input'], ensure_ascii=False)})` → "
        f"{t['output_snippet'].replace(chr(10), ' ')[:200]}"
        for t in tools
    )


def fmt_scores(j):
    s = f"tool={j['tool_choice']} acc={j['accuracy']} rel={j['relevance']} tone={j['tone']}"
    if j.get("context_retention") is not None:
        s += f" ctx={j['context_retention']}"
    return s


def fmt_issues(j):
    return "\n".join(f"- ⚠️ {i}" for i in j.get("critical_issues") or []) or "_(none)_"


def fmt_single(num, r, j):
    return (
        f"\n#### S{num} · _{r['category']}_ · **{r['speaker']}**: \"{r['question']}\"\n\n"
        f"**Tools:**\n{fmt_tools(r['tools'])}\n\n"
        f"**Response:** {r['reply']}\n\n"
        f"**Scores:** `{fmt_scores(j)}` — {j['notes']}\n\n"
        f"**Issues:**\n{fmt_issues(j)}\n"
    )


def fmt_multi(num, conv, turns, judgments):
    lines = [f"\n#### M{num} · _{conv['category']}_ · **{conv['speaker']}** ({len(turns)} turns)\n"]
    for i, (t, j) in enumerate(zip(turns, judgments), 1):
        lines.append(f"**T{i}** \"{t['question']}\"")
        lines.append(f"_Tools:_ {fmt_tools(t['tools'])}")
        lines.append(f"_Response:_ {t['reply']}")
        lines.append(f"_Scores:_ `{fmt_scores(j)}` — {j['notes']}")
        iss = fmt_issues(j)
        if iss != "_(none)_":
            lines.append(f"_Issues:_ {iss}")
        lines.append("")
    return "\n".join(lines)


def round_summary(singles, multis):
    all_j = [j for _, j in singles]
    for _, _, jl in multis:
        all_j.extend(jl)
    n = len(all_j)
    def avg(k):
        vals = [j[k] for j in all_j if j.get(k) is not None]
        return round(sum(vals) / len(vals), 2) if vals else 0
    fails = sum(
        1 for j in all_j
        if min(j["tool_choice"], j["accuracy"], j["relevance"], j["tone"]) <= 3
    )
    crit = sum(len(j.get("critical_issues") or []) for j in all_j)
    avg_ctx = avg("context_retention")
    ctx_note = f"  context_retention={avg_ctx}" if avg_ctx else ""
    return (
        f"\n### Round summary\n"
        f"- {n} interactions ({len(singles)} single + "
        f"{sum(len(t) for _, t, _ in multis)} multi-turn)\n"
        f"- Avg scores: tool={avg('tool_choice')}  accuracy={avg('accuracy')}  "
        f"relevance={avg('relevance')}  tone={avg('tone')}{ctx_note}\n"
        f"- Interactions with any score ≤3: **{fails}**\n"
        f"- Critical issues flagged: **{crit}**\n"
    )


# ── Main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", type=int, required=True)
    ap.add_argument("--note", type=str, default="")
    ap.add_argument("--n-single", type=int, default=10)
    ap.add_argument("--n-multi", type=int, default=3)
    args = ap.parse_args()

    print(f"Round {args.round}: generating {args.n_single} single + {args.n_multi} multi…")
    t0 = time.time()
    qs = generate_questions(args.round, args.n_single, args.n_multi)
    print(f"  questions generated in {time.time()-t0:.1f}s")

    print("Running single-turns…")
    singles = []
    for i, q in enumerate(qs["single"], 1):
        cid = f"r{args.round}-s{i}-{uuid.uuid4().hex[:6]}"
        r = run_single(q, cid)
        j = judge_interaction(r["question"], r["speaker"], r["tools"], r["reply"])
        singles.append((r, j))
        print(f"  S{i} [{q['category']}] {fmt_scores(j)}")

    print("Running multi-turn convos…")
    multis = []
    for i, conv in enumerate(qs["multi"], 1):
        cid = f"r{args.round}-m{i}-{uuid.uuid4().hex[:6]}"
        turns = run_multi(conv, cid)
        judgments = []
        prior = []
        for t in turns:
            j = judge_interaction(t["question"], t["speaker"], t["tools"], t["reply"], prior_turns=prior)
            judgments.append(j)
            prior.append({"question": t["question"], "reply": t["reply"]})
        multis.append((conv, turns, judgments))
        tool_avg = sum(j["tool_choice"] for j in judgments) / len(judgments)
        print(f"  M{i} [{conv['category']}] {len(turns)} turns, avg tool={tool_avg:.1f}")

    # ── Write log ────────────────────────────────────────────────────────────
    when = datetime.now(timezone.utc).isoformat(timespec="seconds")
    parts = [f"\n## Round {args.round} · {when}\n"]
    if args.note:
        parts.append(f"**Changes since previous round:** {args.note}\n")
    parts.append("\n### Single-turn interactions\n")
    for i, (r, j) in enumerate(singles, 1):
        parts.append(fmt_single(i, r, j))
    parts.append("\n### Multi-turn conversations\n")
    for i, (c, t, j) in enumerate(multis, 1):
        parts.append(fmt_multi(i, c, t, j))
    parts.append(round_summary(singles, multis))

    if not LOG_PATH.exists():
        LOG_PATH.write_text(
            "# Oliver test log\n\n"
            "End-to-end evaluation of Oliver via `tests/eval.py`. Each round generates "
            "questions through Sonnet, runs them through Oliver's agent loop with tool-call "
            "tracing, and judges the result via Sonnet. Code changes between rounds are "
            "noted at the top of each round.\n"
        )
    with LOG_PATH.open("a") as f:
        f.write("\n".join(parts))
    print(f"\nAppended round {args.round} to {LOG_PATH} ({time.time()-t0:.1f}s total)")


if __name__ == "__main__":
    main()
