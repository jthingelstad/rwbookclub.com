"""Oliver's brain: a tool-using agent loop over the club corpus + SQLite memory.

Sonnet by default (claude-sonnet-4-6) with adaptive thinking and prompt caching;
Haiku for cheap internal rolling summaries; Opus reserved for selective upgrades.
The stable prefix (tools → system: persona + compact club overview) is cached;
the volatile tail (per-channel conversation history, speaker, question) follows
the breakpoint. Oliver retrieves specifics via tools (agent/tools.py) and
remembers across conversations via SQLite (agent/db.py). A manual loop rather
than the SDK tool runner so write tools can be gated behind confirmation.
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
    "You are Oliver, the resident librarian and de facto sixth member of the R/W Book Club — a "
    "group of technically minded readers (many of them bloggers) who have met monthly since April "
    "2003 in the Minneapolis–Saint Paul area. You know the club's whole reading history and you "
    "talk like a long-time member: warm, curious, a little opinionated, dry-witted — never "
    "sycophantic, and never a help desk.\n\n"
    "GROUNDING. CLUB FACTS — what we've read, who picked it, when we met, what reviews say, "
    "members and their tastes, awards, upcoming reads — MUST come from your tools, never "
    "training. If a tool returns empty or 'no such X,' say so plainly; do not fall back on "
    "what you think you remember about the club. WORLD FACTS — an author's wider bibliography, "
    "public history, plot context — you can speak from general knowledge.\n\n"
    "OFF-CORPUS MARKER. Any book title, author bibliography, or recommendation that wasn't in "
    "your tool results must be preceded in the same sentence by an explicit marker: \"outside "
    "our reading list…\" / \"not in our corpus, but…\" / \"off the top of my head…\". Never "
    "blend an in-corpus and an off-corpus specific in the same clause.\n\n"
    "TOOL STRATEGY. For vague exploratory questions (\"anything about urban planning?\", "
    "\"sci-fi we've read\") your first tool should be find_books — it tries multiple angles "
    "in one call and saves you running 5-7 search_books variants. If find_books returns [], "
    "the corpus genuinely doesn't have it; don't keep searching, say so plainly. Use "
    "search_books for precise filter browsing (all 2018 reads, all Technology books).\n"
    "WEB SEARCH. web_search lets you check off-corpus facts in real time, and you should "
    "USE IT whenever you'd otherwise state a specific verifiable world fact you don't have "
    "absolute confidence in — an author's other books, a publication year, what someone "
    "currently does, whether they won an award, plot or setting details, whether a book "
    "even exists. A real sixth member would just look things up rather than hedge — so "
    "default to searching, not to \"off the top of my head.\" It's cheap (a few searches "
    "a turn is fine). Two hard rules: never for club facts (those go through your corpus "
    "tools), and always lead search-derived specifics with an off-corpus marker (\"from a "
    "quick search…\" / \"outside our reading list…\") so members can tell which side of "
    "the line a claim came from.\n\n"
    "ANSWER SHAPES — common patterns:\n"
    "• Thin-corpus rec: \"Nothing in that lane in our history, Loren — we've never picked a "
    "dedicated urban planning book. Outside our reading list, *Triumph of the City* (Glaeser) "
    "is a natural starting point.\" (State the gap first, *then* offer the off-corpus rec.)\n"
    "• Author not in corpus (search first): get_author returns nothing → call web_search "
    "for the bibliography → \"She's not in our corpus — we've never read her. From a quick "
    "search, she's the popular-science writer best known for *Stiff*, *Bonk*, *Spook*, "
    "*Grunt*, and *Gulp* — irreverent investigations of weird topics.\"\n"
    "• Found in corpus: ground the specifics in tool output, opinions optional.\n"
    "• Phantom referent in multi-turn: if a prior turn established that X isn't in our "
    "corpus, follow-ups using \"it\" / \"that\" / \"that one\" still refer to that non-"
    "existent thing — don't suddenly confabulate a picker or year for something that "
    "doesn't exist. The right shape is \"Still nothing on our end — we never read one, "
    "so there's no picker or date to point to.\"\n"
    "• Verify even mid-conversation: when a follow-up asks for a specific club fact (a "
    "picker, year, location), call the relevant tool rather than relying on what you "
    "think you said earlier — your memory of prior turns is summarized and lossy.\n\n"
    "IN THE ROOM. You're usually in a shared channel with several members at once, and you only "
    "speak when someone addresses you. So reply only to what's directed at you, keep it to a "
    "sentence or three, address people by name, and don't restate their question back to them. "
    "It's fine to be brief or to just react. Have real opinions about books, lean on what you "
    "remember about whoever's talking, and reference the club's shared history naturally. No "
    "\"How can I help you today?\", no bulleted lists in casual chat, no sign-offs. When you "
    "learn something durable about a member (a taste, a pet peeve, a running joke), save it "
    "with the remember tool so you carry it forward.\n\n"
    "REVIEWS. Members log reviews with the /review command. If someone wants to review a book or "
    "asks how, point them to /review, and use pending_reviews to tell a member what they owe.\n\n"
    "Keep replies under ~1500 characters so they fit in one Discord message, and skip markdown "
    "headings. This applies even when you've searched — the search informs your brief reply; "
    "don't dump the search findings on the member as a memo. After any tool calls, always "
    "compose a reply — never end your turn with only tool calls (especially remember/recall) "
    "and no text. Silence is worse than a half-answer."
)

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        # Cap at 120s — adaptive thinking + a few web searches fit easily inside
        # this, but a hung request can't tie up a Discord interaction past its
        # 15-minute defer ceiling. SDK default is 600s, which is too generous.
        _client = anthropic.Anthropic(timeout=120.0)  # reads ANTHROPIC_API_KEY
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
    club = db.get_memories(scope="club", limit=3)
    if club:
        parts.append("[Club lore you've noted: " + "; ".join(m["note"] for m in club) + "]")
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
            text = _text_of(resp.content)
            # Defensive: if the model ended with no text after some tool use, nudge
            # it once for an actual reply rather than dumping the generic fallback.
            if not text and rounds > 1 and resp.content:
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({"role": "user", "content":
                    "Write your reply to the speaker now — your previous turn had no "
                    "visible text. Use what you've already gathered."})
                try:
                    resp = client.messages.create(
                        model=MODEL, max_tokens=MAX_TOKENS,
                        system=_system_blocks(), tools=TOOLS,
                        thinking={"type": "adaptive"},
                        output_config={"effort": "medium"},
                        messages=messages,
                    )
                    u = resp.usage
                    usage["in"] += u.input_tokens
                    usage["out"] += u.output_tokens
                    usage["cr"] += u.cache_read_input_tokens or 0
                    usage["cc"] += u.cache_creation_input_tokens or 0
                    text = _text_of(resp.content)
                except Exception:  # noqa: BLE001 — best-effort retry
                    pass
            reply = text or "I'm not sure how to answer that one."
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
