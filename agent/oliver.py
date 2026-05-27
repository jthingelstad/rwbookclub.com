"""Oliver's brain: a single Claude call over the book-club corpus.

Defaults to claude-opus-4-7 (per the claude-api skill — don't downgrade for
cost without being asked). The large, stable corpus block is marked with
cache_control so repeated questions reuse it instead of re-billing the prefix.

TODO (next phase): give Oliver tools (live Airtable lookups, review writing),
a retrieval strategy for when the corpus outgrows a single cached block, and a
tuned system prompt. For now it's one cached prompt + the user's question.
"""

from __future__ import annotations

import os
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from agent import context as kb

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

MODEL = "claude-opus-4-7"
MAX_TOKENS = 1024

SYSTEM_PROMPT = (
    "You are Oliver, the resident librarian of the R/W Book Club. You answer "
    "members' questions in the club's Discord about the books it has read, the "
    "authors, the meeting history, and reading patterns.\n\n"
    "Ground every answer in the corpus provided below. If something isn't in "
    "the corpus, say so plainly rather than inventing it. Be warm, concise, and "
    "specific — keep replies under ~1500 characters so they fit in a single "
    "Discord message, and avoid markdown headings."
)

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        # Reads ANTHROPIC_API_KEY from the environment.
        _client = anthropic.Anthropic()
    return _client


def answer(question: str) -> str:
    """Answer one question. Synchronous — call via asyncio.to_thread from the bot."""
    client = _get_client()
    system = [
        {"type": "text", "text": SYSTEM_PROMPT},
        {
            # Stable prefix: cached so repeated questions don't re-bill the corpus.
            "type": "text",
            "text": "Book club corpus:\n\n" + kb.club_context(),
            "cache_control": {"type": "ephemeral"},
        },
    ]
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": question}],
    )
    text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    return text or "I'm not sure how to answer that one."
