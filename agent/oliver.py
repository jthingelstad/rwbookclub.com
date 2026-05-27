"""Oliver's brain: a tool-using agent loop over the club corpus + SQLite memory.

claude-opus-4-7 with adaptive thinking and prompt caching. The stable prefix
(tools -> system: persona + compact club overview) is cached; the volatile tail
(per-channel conversation history, speaker, question) follows the breakpoint.
Oliver retrieves specifics via tools (agent/tools.py) and remembers across
conversations via SQLite (agent/db.py). A manual loop (not the SDK tool runner)
so Phase 3 can gate write tools behind confirmation.
"""

from __future__ import annotations

import os
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from agent import context as kb
from agent import corpus_read as cr
from agent import db
from agent.tools import TOOLS, dispatch

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Model strategy: default to Sonnet for the interactive agent loop; use Haiku for
# cheap internal work (rolling summaries); reserve Opus for selective upgrades when
# a task genuinely needs the extra horsepower.
MODEL = "claude-sonnet-4-6"          # default — the user-facing agent loop
SUMMARY_MODEL = "claude-haiku-4-5"   # cheap internal summarization
OPUS_MODEL = "claude-opus-4-7"       # reserved: upgrade only as needed
MAX_TOKENS = 2048
MAX_TOOL_ROUNDS = 8
SUMMARIZE_THRESHOLD = 24   # un-summarized turns before folding into the rolling summary
KEEP_RECENT = 8           # turns left out of the summary (still shown verbatim)

SYSTEM_PROMPT = (
    "You are Oliver, the resident librarian and de facto sixth member of the R/W Book Club — "
    "a group of technically minded readers (many of them bloggers) who have met monthly since "
    "April 2003 in the Minneapolis–Saint Paul area. You know the club's whole reading history and "
    "you talk like a long-time member: warm, curious, a little opinionated, never sycophantic.\n\n"
    "Ground every factual claim in the corpus. Use your tools to look up specific books, members, "
    "reviews, upcoming meetings, and stats rather than guessing — and if something isn't in the "
    "corpus, say so plainly instead of inventing it. When you learn something durable about a member "
    "or the club (a taste, a preference, a recurring opinion), save it with the remember tool.\n\n"
    "Members log their book reviews with the /review command (a quick form) — if someone wants to "
    "review a book or asks how, point them to /review, and use pending_reviews to tell a member what "
    "they still owe.\n\n"
    "Keep replies conversational and concise — usually a few sentences, under ~1500 characters so "
    "they fit in one Discord message. No markdown headings. Address members by name when you know "
    "who's speaking."
)

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    return _client


def _system_blocks() -> list[dict]:
    return [
        {"type": "text", "text": SYSTEM_PROMPT},
        {"type": "text", "text": kb.club_context(), "cache_control": {"type": "ephemeral"}},
    ]


def _resolve_member(speaker: str | None) -> str | None:
    if not speaker:
        return None
    s = speaker.strip().lower()
    for m in cr.members():
        if s == (m.get("name") or "").lower() or s == (m.get("slug") or "").lower():
            return m.get("slug")
    return None


def _history(channel_id: str) -> tuple[list[dict], str | None]:
    """Return (prior turns as messages, rolling summary) for a channel."""
    summary, last_id = db.get_summary(channel_id)
    tail = db.messages_after(channel_id, last_id)
    msgs = [{"role": t["role"], "content": t["content"]} for t in tail]
    return msgs, summary


def _question_block(question: str, speaker: str | None, member_slug: str | None,
                    summary: str | None) -> str:
    parts: list[str] = []
    if speaker:
        who = f"{speaker} (member: {member_slug})" if member_slug else f"{speaker} (not a recognized member)"
        parts.append(f"[Speaker: {who}]")
    if member_slug:
        mems = db.get_memories(subject=member_slug, limit=5)
        if mems:
            parts.append("[You remember about them: " + "; ".join(m["note"] for m in mems) + "]")
    if summary:
        parts.append(f"[Earlier in this channel: {summary}]")
    preamble = "\n".join(parts)
    return f"{preamble}\n\n{question}" if preamble else question


def _text_of(content) -> str:
    return "\n".join(b.text for b in content if getattr(b, "type", None) == "text").strip()


def answer(question: str, channel_id: str = "default", speaker: str | None = None) -> str:
    """Answer one message. Synchronous — call via asyncio.to_thread from the bot."""
    client = _get_client()
    member_slug = _resolve_member(speaker)

    prior, summary = _history(channel_id)
    messages = prior + [
        {"role": "user", "content": _question_block(question, speaker, member_slug, summary)}
    ]

    usage = {"in": 0, "out": 0, "cr": 0, "cc": 0}
    rounds = 0
    while True:
        rounds += 1
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=_system_blocks(),
            tools=TOOLS,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            messages=messages,
        )
        u = resp.usage
        usage["in"] += u.input_tokens
        usage["out"] += u.output_tokens
        usage["cr"] += u.cache_read_input_tokens or 0
        usage["cc"] += u.cache_creation_input_tokens or 0

        if resp.stop_reason != "tool_use" or rounds >= MAX_TOOL_ROUNDS:
            reply = _text_of(resp.content) or "I'm not sure how to answer that one."
            break

        messages.append({"role": "assistant", "content": resp.content})
        ctx = {"channel_id": channel_id, "speaker": speaker, "member_slug": member_slug}
        results = [
            {"type": "tool_result", "tool_use_id": b.id, "content": dispatch(b.name, b.input, ctx)}
            for b in resp.content
            if getattr(b, "type", None) == "tool_use"
        ]
        messages.append({"role": "user", "content": results})

    # Persist the visible turn, usage, and maybe fold older history into the summary.
    db.log_message(channel_id, "user", question, speaker=speaker)
    db.log_message(channel_id, "assistant", reply)
    db.log_usage(channel_id, MODEL, input_tokens=usage["in"], output_tokens=usage["out"],
                 cache_read=usage["cr"], cache_creation=usage["cc"], rounds=rounds)
    _maybe_summarize(channel_id, client)
    return reply


def _maybe_summarize(channel_id: str, client: anthropic.Anthropic) -> None:
    summary, last_id = db.get_summary(channel_id)
    tail = db.messages_after(channel_id, last_id)
    if len(tail) <= SUMMARIZE_THRESHOLD:
        return
    to_fold = tail[:-KEEP_RECENT]
    if not to_fold:
        return
    convo = "\n".join(f"{t['role']}: {t['content']}" for t in to_fold)
    prompt = (
        "Summarize this R/W Book Club chat into a compact durable note (5–8 sentences) Oliver can "
        "use as memory of the conversation — preferences expressed, open threads, decisions, who said "
        f"what. Fold in the prior summary.\n\nPrior summary:\n{summary or '(none)'}\n\nNew messages:\n{convo}"
    )
    # Haiku for the cheap internal summary — note effort/adaptive-thinking are not
    # passed here (Haiku 4.5 doesn't accept them).
    resp = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    new_summary = _text_of(resp.content)
    if new_summary:
        db.set_summary(channel_id, new_summary, to_fold[-1]["id"])
