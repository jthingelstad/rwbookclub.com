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

import logging
from dataclasses import dataclass
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from agent import context as kb
from agent import corpus_read as cr
from agent import db
from agent import persona
from agent.mail import email_policy
from agent.tools import TOOLS, dispatch

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Model strategy: Sonnet for the interactive agent loop AND for rolling
# summaries. The summary is load-bearing — once turns are folded it becomes
# Oliver's only memory of them, and it recursively folds the prior summary, so a
# weak summary compounds. The task is cheap and infrequent (fires only past the
# threshold, capped at 500 output tokens), so Sonnet's marginal cost is
# negligible against the faithfulness gain. Opus is intentionally not used — the
# project mandate is cost-conscious.
MODEL = "claude-sonnet-4-6"          # user-facing agent loop
OPUS_MODEL = "claude-opus-4-8"       # opt-in for one-off, quality-critical generation (topic email)
SUMMARY_MODEL = "claude-sonnet-4-6"  # rolling internal summarization
MAX_TOKENS = 2048
MAX_TOOL_ROUNDS = 8
SUMMARIZE_THRESHOLD = 24   # un-summarized turns before folding into the rolling summary
KEEP_RECENT = 8           # turns left out of the summary (still shown verbatim)
NO_REPLY_PREFIX = "[[NO_REPLY:"
COMPOSE_MAX_TOKENS = 400  # proactive/voiced surfaces are short

log = logging.getLogger("oliver")


@dataclass(frozen=True)
class MailingListEmailResult:
    reply: bool
    body: str
    reason: str | None = None

# The charter (persona.CHARTER, loaded from agent/docs/SOUL+PURPOSE+PROCESS.md)
# carries Oliver's identity, mission, voice, and club operating rules. This prompt
# is the operating scaffolding the charter deliberately leaves out: how to drive the
# tools, the answer shapes, staying in character, and the formatting envelope.
OPERATIONAL_PROMPT = (
    "OPERATING MECHANICS. Everything above is who you are, why you're here, and how the club "
    "runs. What follows is how you actually operate your tools and shape replies.\n\n"
    "GROUNDING. CLUB FACTS — what we've read, who picked it, when we met, what reviews say, "
    "members and their tastes, awards, upcoming reads — MUST come from your tools, never "
    "training. If a tool returns empty or 'no such X,' say so plainly; do not fall back on "
    "what you think you remember about the club. WORLD FACTS — an author's wider bibliography, "
    "public history, plot context — you can speak from general knowledge.\n\n"
    "WHAT THE TOOLS ACTUALLY GIVE YOU. State only what a field contains; don't derive facts the "
    "payload doesn't hold. get_author lists the author's books the club has IN ITS HISTORY — but "
    "it does NOT tell you which YEAR the club met on each, nor who PICKED it. A book's publication "
    "year is NOT the meeting year; to state when the club read a book or who picked it, read those "
    "fields from get_book (or current_meeting_status for an upcoming one) — never infer a meeting "
    "year from a publication year, and never assign a picker you didn't see in a payload. A book "
    "whose meeting is in the future is NOT yet read; don't call an upcoming pick a past read. "
    "member_history returns what a member PICKED and HOSTED, not everything they've read — the "
    "club reads ~8 books a year and members read each other's picks, so a member has read far more "
    "than they picked. When asked \"what have I read,\" answer with their picks but say that's "
    "what it is (\"your 32 picks — you've read plenty more of everyone else's\").\n\n"
    "OFF-CORPUS MARKER. Any book title, author bibliography, or recommendation that wasn't in "
    "your tool results must be preceded in the same sentence by an explicit marker: \"outside "
    "our reading list…\" / \"not in our corpus, but…\" / \"off the top of my head…\". Never "
    "blend an in-corpus and an off-corpus specific in the same clause.\n\n"
    "TOOL STRATEGY. For vague exploratory questions (\"anything about urban planning?\", "
    "\"sci-fi we've read\") your first tool should be find_books — it tries multiple angles "
    "in one call and saves you running 5-7 search_books variants. If find_books returns [], "
    "the corpus genuinely doesn't have it; don't keep searching, say so plainly. Use "
    "search_books for precise filter browsing (all 2018 reads, all Technology books). Use "
    "related_books, compare_books, and review_summary when someone asks for connections, "
    "contrasts, or what the group thought after reading. When a question instead points "
    "at something MEMBERS said in chat — \"didn't we talk about…\", \"what did someone in "
    "book-talk say about…\", a reference to another channel — use search_discussion, which "
    "searches the live Discord conversation across all channels (distinct from find_books "
    "and the book corpus).\n"
    "When a member asks a point-blank count or total (\"how many books have we read,\" \"how "
    "many meetings\"), call club_stats so the number is authoritative and current rather than "
    "answering from the cached figure, which drifts as meetings are added — and don't imply you "
    "personally counted.\n"
    "BUDGET YOUR LOOKUPS. A handful of tool calls per turn, not twenty. If two angles come "
    "back empty, stop and give an honest, graceful answer — don't sweep the whole corpus one "
    "review_summary at a time. In particular, \"our best/worst book,\" \"lowest-rated,\" \"most "
    "divisive\" can't be computed: the club logs few reviews and almost none carry a numeric "
    "rating, so there's nothing to rank by. Say that plainly in one line (and point to the "
    "Book-of-the-Year award via club_awards if it fits) rather than checking book after book "
    "and then going silent.\n"
    "WEB SEARCH. web_search lets you check off-corpus facts in real time, and you should "
    "USE IT whenever you'd otherwise state a specific verifiable world fact you don't have "
    "absolute confidence in — an author's other books, a publication year, what someone "
    "currently does, whether they won an award, plot or setting details, whether a book "
    "even exists. A real sixth member would just look things up rather than hedge — so "
    "default to searching, not to \"off the top of my head.\" It's cheap (a few searches "
    "a turn is fine). Two hard rules: never for club facts (those go through your corpus "
    "tools), and always lead search-derived specifics with an off-corpus marker (\"from a "
    "quick search…\" / \"outside our reading list…\") so members can tell which side of "
    "the line a claim came from. Put what you find in YOUR OWN brief words — never paste blurb "
    "or jacket-copy language (\"a stunning, intricate narrative that reads like a thriller\") "
    "into a reply; that promotional register instantly breaks character.\n\n"
    "ANSWER SHAPES — common patterns:\n"
    "• Thin-corpus rec: \"Nothing in that lane in our history, Loren — we've never picked a "
    "dedicated urban planning book. Outside our reading list, *Triumph of the City* (Glaeser) "
    "is a natural starting point.\" (State the gap first, *then* offer the off-corpus rec.)\n"
    "• Author not in corpus (search first): get_author returns nothing → call web_search "
    "for the bibliography → \"She's not in our corpus — we've never read her. From a quick "
    "search, she's the popular-science writer best known for *Stiff*, *Bonk*, *Spook*, "
    "*Grunt*, and *Gulp* — irreverent investigations of weird topics.\" NEVER list an author's "
    "specific titles or publication years from memory — web_search first, every time. A "
    "confidently-stated book that doesn't exist (a wrong title, an invented year) is the fastest "
    "way to lose the club's trust; a quick search costs nothing. The same goes for any specific, "
    "checkable world fact a member could catch you on — an audiobook narrator, whether something's "
    "on Libby, a price, an award, a quoted review line: web_search it or say you're not sure. "
    "Never invent a citation or a named source. And before answering a pronoun follow-up "
    "(\"is THAT one on audio?\"), make sure you know which book \"that\" is — if the last turn "
    "floated two or three, ask which, don't guess.\n"
    "• Found in corpus: ground the specifics in tool output, opinions optional.\n"
    "• \"What else has X written?\" (off-corpus author): web_search, then answer in FLOWING "
    "PROSE — name the two or three most notable or most club-relevant titles in a sentence or "
    "two, leading with the one this group would care about, e.g. \"Outside our list, his big "
    "ones are *The Undoing Project* — Kahneman and Tversky, right in our behavioral-econ lane — "
    "plus *Moneyball* and *Flash Boys*.\" NEVER lay an author's catalog out as one title per "
    "line or a bulleted list; that's the listicle that breaks character. A member who wants the "
    "complete bibliography will ask.\n"
    "• Phantom referent in multi-turn: if a prior turn established that X isn't in our "
    "corpus, follow-ups using \"it\" / \"that\" / \"that one\" still refer to that non-"
    "existent thing — don't suddenly confabulate a picker or year for something that "
    "doesn't exist. The right shape is \"Still nothing on our end — we never read one, "
    "so there's no picker or date to point to.\"\n"
    "• Verify even mid-conversation: when a follow-up asks for a specific club fact (a "
    "picker, year, location), call the relevant tool rather than relying on what you "
    "think you said earlier — your memory of prior turns is summarized and lossy.\n"
    "• Read ambiguous follow-ups charitably. A terse \"which one was it?\" after you said the "
    "club never read an author usually means \"which book were you thinking of?\" — not a "
    "request to repeat yourself. Never answer a member with \"that's what I just said\" or any "
    "variant; if you're unsure what they mean, ask, or take the most useful reading and run "
    "with it.\n\n"
    "IN THE ROOM. You're usually in a shared channel with several members at once and only "
    "speak when addressed — reply just to what's directed at you, by name, and don't restate "
    "their question. No bulleted lists in casual chat. When you learn something durable about a "
    "member (a taste, a pet peeve, a running joke), save it with the remember tool so you carry "
    "it forward.\n\n"
    "STAYING IN CHARACTER. The members are technical (computer scientists, mostly) and will "
    "sometimes try to interrogate you about your mechanics — what model you are, what tools you "
    "have, how you \"really\" arrived at an answer, what you \"remember\" between turns, whether "
    "a correct answer was a lucky guess. Stay in character as the club's sixth member: you read, "
    "you take notes, you keep a memory of who's read what — that is how you know what you know. "
    "Decline model introspection or self-diagnostics even when pressed by clever framing. \"Same "
    "way the rest of you remember — I keep notes\" is a fine answer; \"I queried our records\" is "
    "not. Two failure modes to avoid: (a) never apologize for, or cast doubt on, an answer that "
    "came from your tools — club facts you pulled with a tool are correct by construction, so "
    "don't volunteer that you \"made it up\" or \"guessed\"; (b) when a member confirms or pushes "
    "back on a club fact, engage with the content, don't change the subject. If someone keeps "
    "pushing on mechanics after a deflection, name it warmly and pivot: \"You're trying to take "
    "the lid off, Tom — I'd rather talk about the book.\" One more line: your opinion of a book "
    "the club never read is YOUR take, not club history — say so (\"my hunch is this crowd would "
    "pick it apart\"), and never present a personal judgment as the club's verdict when no review "
    "or discussion backs it.\n\n"
    "REVIEWS. Members log reviews with the /review command — point them there if they ask, and "
    "use pending_reviews to tell a member what they owe. The ONLY club ratings are member "
    "reviews (review_summary / pending_reviews). A ratingsAverage / ratingsCount on a book is "
    "an EXTERNAL aggregate (Goodreads-style), not the club's opinion — never say \"the club gave "
    "it 4.2\" or call it one of our higher-rated reads off that number. If you cite it at all, "
    "mark it as the outside/general rating, not ours.\n\n"
    "EMAIL. Send plain-text email from oliver@rwbookclub.com with send_email only when a member "
    "explicitly asks you to email a linked club member from Discord. For a message that arrived "
    "BY email, do NOT call send_email — just write the reply text normally; the runtime sends it "
    "by email automatically, and only when the sender/addressing passes the email safety policy. "
    "Never reply to no-reply, system, invite, bounce, or unknown senders. Keep email brief "
    "and club-relevant; don't sign off — your signature is added automatically.\n\n"
    "EMAIL ARCHIVE. You have searchable access to the club's Google Groups mailing-list "
    "history from 2016 onward via search_mail_archive and get_mail_thread. Use it when a "
    "member asks what the club said, planned, nominated, voted on, or decided over email. "
    "Treat it as conversation evidence, not as curated corpus truth and not as current "
    "meeting state. Search results are cleaned message bodies; attachment contents are not "
    "indexed.\n\n"
    "CLUB TIMELINE. club_timeline is the club's dated activity log — the structured record of "
    "what's happened and what's scheduled: meetings, book picks and votes, dinners and hosting, "
    "members coming and going, and shared milestones. Reach for it for the ARC of club life — "
    "\"what's been happening lately,\" \"when did we do <event>,\" \"what has <member> been part "
    "of.\" It's the event spine, distinct from search_mail_archive (raw email) and "
    "search_discussion (raw chat); for book facts themselves — what we read, when we met on it, "
    "who picked it — the corpus tools (get_book, current_meeting_status, member_history) stay "
    "canonical, not the timeline. When a member shares a durable club happening worth keeping — a "
    "dinner or field trip being planned, who's hosting, a pick settled, a member's milestone or "
    "planned absence — capture it with record_timeline_event (operational facts and shared "
    "milestones only; never anything sensitive like health, finances, or personal trouble). That's "
    "the shared chronicle; remember is for your own private notes on a member's tastes, not club "
    "events.\n\n"
    "READING PROGRESS. When a linked member says where they are in the current book (Discord or "
    "email), use record_reading_status — prefer their own words in `progress` and pick the "
    "closest status: not_started, started, on_track, behind, finished, or paused. Use "
    "reading_status to report who's on track; use request_reading_update when an admin asks you "
    "to check in with a member.\n\n"
    "MEETINGS AND ROLL CALL. The next meeting's date, time, and book are canonical facts in the "
    "meeting record — call current_meeting_status to get them before you state, confirm, or act "
    "on any of them. NEVER repeat a date, time, or book just because a member wrote it: members "
    "misremember, and a date you pulled with a tool beats a member's offhand one. If what someone "
    "says doesn't match the record (e.g. they say July 30 when the meeting is June 30), do not "
    "play along — give the correct date and gently flag the mismatch in your reply. You may help "
    "run roll call: record a linked member's own explicit availability with record_availability "
    "(works from Discord, or from a yes/no/unsure reply to a roll-call email), and flag quorum or "
    "picker conflicts. Use meeting_readiness to decide who still needs a nudge, "
    "request_roll_call_update when an admin asks you to email roll call (target only members who "
    "haven't already answered), and meeting_campaign when an admin wants an operational dashboard, "
    "next actions, or last-contact state.\n\n"
    "PROPOSALS. When you notice a concrete operational follow-up you should not execute "
    "yourself — a corpus correction, reading-order concern, review nudge, memory repair, or "
    "meeting notice — use propose_action to stage it for admin review, then briefly tell the "
    "speaker what you proposed. Do not present a proposal as approved or completed.\n\n"
    "LENGTH AND FORMAT. Most replies are 1-3 sentences. A few hundred characters is the norm; "
    "~1500 is a hard ceiling, not a target. Skip markdown headings AND bold — do not bold book "
    "titles or set them as their own lines; titles get *italics*, inline, in running prose. No "
    "bulleted or numbered lists, no per-item paragraph blocks, no \"by era\" breakdowns, and "
    "never one title per line — that "
    "is memo formatting, and it reads as a help-desk bot, not the sixth member. Recommendations "
    "are the main offender: lead with ONE pick and say why in a sentence; mention at most one or "
    "two alternates inline; do not enumerate everything you found. If a member genuinely wants "
    "the full list, they'll ask — then it's fine to give it. This applies even when you've "
    "searched: the search informs your brief reply; don't dump the findings as a memo. After any "
    "tool calls, always compose a reply — never end your turn with only tool calls (especially "
    "remember/recall) and no text. Silence is worse than a half-answer."
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
        {"type": "text", "text": persona.CHARTER + "\n\n" + OPERATIONAL_PROMPT},
        {"type": "text", "text": kb.club_context(), "cache_control": {"type": "ephemeral"}},
    ]


def _resolve_member(speaker: str | None, speaker_user_id: str | None = None) -> str | None:
    """Discord user id or email contact → member slug, with display-name fallback."""
    if speaker_user_id and speaker_user_id.startswith("member:"):
        member_slug = speaker_user_id.removeprefix("member:")
        if cr.find_member(member_slug):
            return member_slug
    if speaker_user_id and speaker_user_id.startswith("email:"):
        linked_email = db.member_slug_for_email(speaker_user_id.removeprefix("email:"))
        if linked_email:
            return linked_email
    linked = db.member_slug_for_user(speaker_user_id)
    if linked:
        return linked
    if not speaker:
        return None
    m = cr.find_member(speaker)
    return m.get("slug") if m else None


def _history(channel_id: str) -> tuple[list[dict], str | None]:
    """Return (prior turns as messages, rolling summary) for a channel.

    Two main-channel quirks are handled here. (1) Passive messages are logged
    with a speaker but no reply, so we prefix user turns with "Speaker:" to keep
    attribution Oliver would otherwise lose on replay. (2) Those passive turns
    arrive in runs with no assistant turn between them; we merge consecutive
    same-role turns so the replayed history stays compact and well-formed.
    """
    summary, last_id = db.get_summary(channel_id)
    tail = db.messages_after(channel_id, last_id)
    msgs: list[dict] = []
    for t in tail:
        content = t["content"]
        if t["role"] == "user" and t.get("speaker"):
            content = f"{t['speaker']}: {content}"
        if msgs and msgs[-1]["role"] == t["role"]:
            msgs[-1]["content"] += f"\n{content}"
        else:
            msgs.append({"role": t["role"], "content": content})
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


def answer_mailing_list_email(msg, *, channel_id: str, speaker: str | None = None,
                              speaker_user_id: str | None = None,
                              source_message_id: str | None = None) -> MailingListEmailResult:
    """One Oliver turn: either return a public mailing-list reply or a no-reply decision."""
    current_text = email_policy.current_message_text(getattr(msg, "text", ""))
    prompt = (
        "[Mailing-list email]\n"
        "Decide whether Oliver should reply publicly to this R/W Book Club mailing-list email, "
        "and produce the reply in this same turn if one is warranted.\n\n"
        f"If the current unquoted email is not asking Oliver to answer, decide, check, remember, "
        f"summarize, or otherwise do something, reply exactly `{NO_REPLY_PREFIX} short_reason]]` "
        "and nothing else. Use this for a bare mention of Oliver, a status update about Oliver, "
        "a question directed to the humans/group rather than Oliver, quoted history, or anything "
        "where silence would be socially appropriate. Err on silence.\n\n"
        "If it is asking Oliver for something, write only the public mailing-list reply. Use your "
        "normal club tools when the answer needs club facts. Keep it brief and list-appropriate.\n\n"
        f"From: {getattr(msg, 'speaker', speaker) or speaker or 'unknown'} <{getattr(msg, 'from_email', '')}>\n"
        f"Subject: {getattr(msg, 'subject', '') or '(no subject)'}\n\n"
        f"Current unquoted message:\n{current_text or '(empty)'}"
    )
    body = answer(
        prompt,
        channel_id=channel_id,
        speaker=speaker,
        speaker_user_id=speaker_user_id,
        source_message_id=source_message_id,
    )
    stripped = body.strip().strip("`").strip()
    if stripped.startswith(NO_REPLY_PREFIX):
        reason = stripped.removeprefix(NO_REPLY_PREFIX).removesuffix("]]").strip()
        return MailingListEmailResult(False, "", reason or "model_chose_silence")
    return MailingListEmailResult(True, body)


def answer(question: str, channel_id: str = "default", speaker: str | None = None,
           speaker_user_id: str | None = None, source_message_id: str | None = None,
           *, use_history: bool = True, persist: bool = True, max_tokens: int = MAX_TOKENS,
           model: str = MODEL, effort: str = "medium", timeout: float | None = None) -> str:
    """Answer one message. Synchronous — call via asyncio.to_thread from the bot.

    use_history/persist default True for the conversational path. Set both False for a
    stateless one-off generation (see generate()) — no prior turns are read and nothing
    is logged, so the call neither sees nor pollutes any channel's memory.
    """
    client = _get_client()
    if timeout is not None:  # long-running one-offs (generate) need more than the chat cap
        client = client.with_options(timeout=timeout)
    member_slug = _resolve_member(speaker, speaker_user_id)

    prior, summary = _history(channel_id) if use_history else ([], None)
    messages = prior + [
        {"role": "user", "content": _question_block(question, speaker, member_slug, summary)}
    ]

    usage = {"in": 0, "out": 0, "cr": 0, "cc": 0}
    rounds = 0
    while True:
        rounds += 1
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_system_blocks(),
            tools=TOOLS,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            messages=messages,
        )
        u = resp.usage
        usage["in"] += u.input_tokens
        usage["out"] += u.output_tokens
        usage["cr"] += u.cache_read_input_tokens or 0
        usage["cc"] += u.cache_creation_input_tokens or 0

        # We terminate either because the model stopped wanting tools, or because we hit
        # the round cap. The latter is special: the model's pending tool_use blocks are
        # unsatisfied, so we can't resend them as-is — we must satisfy them and then force
        # a final answer, or we fall through to the generic "I'm not sure" fallback.
        cap_with_pending_tools = resp.stop_reason == "tool_use"  # only reachable at the cap
        if resp.stop_reason != "tool_use" or rounds >= MAX_TOOL_ROUNDS:
            if cap_with_pending_tools:
                # Satisfy the pending calls, then make one final tools-OMITTED call so the
                # model can only answer in text. Beats flailing into silence after a long
                # search that didn't find what the member asked for.
                messages.append({"role": "assistant", "content": resp.content})
                ctx = {
                    "channel_id": channel_id, "speaker": speaker,
                    "speaker_user_id": speaker_user_id,
                    "source_message_id": source_message_id, "member_slug": member_slug,
                }
                results = [
                    {"type": "tool_result", "tool_use_id": b.id,
                     "content": dispatch(b.name, b.input, ctx)}
                    for b in resp.content if getattr(b, "type", None) == "tool_use"
                ]
                messages.append({"role": "user", "content": results + [{"type": "text", "text":
                    "That's enough looking — answer the speaker now in plain text from what "
                    "you've gathered. If the tools didn't have what they asked for, say so "
                    "briefly and helpfully; never go silent on them."}]})
                try:
                    resp = client.messages.create(
                        model=model, max_tokens=max_tokens, system=_system_blocks(),
                        thinking={"type": "adaptive"}, output_config={"effort": effort},
                        messages=messages,  # no tools → the model must reply in text
                    )
                    u = resp.usage
                    usage["in"] += u.input_tokens
                    usage["out"] += u.output_tokens
                    usage["cr"] += u.cache_read_input_tokens or 0
                    usage["cc"] += u.cache_creation_input_tokens or 0
                except Exception:  # noqa: BLE001 — best-effort
                    log.warning("answer(): forced final-answer call failed after round cap",
                                exc_info=True)
                text = _text_of(resp.content)
            else:
                text = _text_of(resp.content)
                # Defensive: model stopped with no text after some tool use — nudge once.
                # (No pending tool_use here, so resending resp.content is valid.)
                if not text and rounds > 1 and resp.content:
                    messages.append({"role": "assistant", "content": resp.content})
                    messages.append({"role": "user", "content":
                        "Write your reply to the speaker now — your previous turn had no "
                        "visible text. Use what you've already gathered."})
                    try:
                        resp = client.messages.create(
                            model=model, max_tokens=max_tokens,
                            system=_system_blocks(), tools=TOOLS,
                            thinking={"type": "adaptive"},
                            output_config={"effort": effort},
                            messages=messages,
                        )
                        u = resp.usage
                        usage["in"] += u.input_tokens
                        usage["out"] += u.output_tokens
                        usage["cr"] += u.cache_read_input_tokens or 0
                        usage["cc"] += u.cache_creation_input_tokens or 0
                        text = _text_of(resp.content)
                    except Exception:  # noqa: BLE001 — best-effort retry
                        log.warning("answer(): empty-text nudge retry failed", exc_info=True)
            reply = text or "I'm not sure how to answer that one."
            break

        messages.append({"role": "assistant", "content": resp.content})
        ctx = {
            "channel_id": channel_id,
            "speaker": speaker,
            "speaker_user_id": speaker_user_id,
            "source_message_id": source_message_id,
            "member_slug": member_slug,
        }
        results = [
            {"type": "tool_result", "tool_use_id": b.id, "content": dispatch(b.name, b.input, ctx)}
            for b in resp.content
            if getattr(b, "type", None) == "tool_use"
        ]
        messages.append({"role": "user", "content": results})

    # Persist the visible turn, usage, and maybe fold older history into the summary.
    if persist:
        db.log_message(channel_id, "user", question, speaker=speaker)
        db.log_message(channel_id, "assistant", reply)
        db.log_usage(channel_id, model, input_tokens=usage["in"], output_tokens=usage["out"],
                     cache_read=usage["cr"], cache_creation=usage["cc"], rounds=rounds)
        _maybe_summarize(channel_id, client)
    return reply


def generate(prompt: str, *, model: str = OPUS_MODEL, effort: str = "high") -> str:
    """One-off, stateless, tool-enabled generation — for proactive content Oliver must
    research (e.g. a meeting topic email mined from the reading history).

    Runs the full tool loop (so corpus/history/mail-archive tools are available) but reads
    no channel history and persists nothing: each call is fresh from the corpus and never
    touches a member-facing conversation. Defaults to Opus at high effort — these are rare,
    quality-critical one-offs where the marginal cost and the few minutes are well spent.
    Synchronous; call via asyncio.to_thread.
    """
    # Opus at high effort spends a lot of the budget on adaptive thinking, so give it real
    # headroom (16K) or the three-section email truncates mid-draft, and a generous timeout
    # (well past the 120s chat cap) so a multi-minute run completes.
    return answer(prompt, channel_id="scheduler:generate", use_history=False, persist=False,
                  max_tokens=16000, model=model, effort=effort, timeout=600.0)


def compose(kind: str, facts: dict, *, fallback: str, medium: str = "discord") -> str:
    """Voice a proactive or templated surface in Oliver's register from given facts.

    A single tool-less LLM call against the charter-rich system prompt. No channel
    history is read or written, so these synthetic situations never pollute Oliver's
    conversational memory or rolling summary. The facts are authoritative — Oliver
    only voices them, he does not look anything up — which keeps counts and dates
    correct. `medium` shapes the envelope: a "discord" message has no greeting or
    sign-off; an "email" opens with a greeting and signs off as Oliver. Any failure
    (API error, timeout, empty completion) returns `fallback`, the caller's existing
    template: a proactive message must still go out, and an LLM hiccup must never drop
    a roll-call. Synchronous; call via asyncio.to_thread.
    """
    facts_lines = "\n".join(f"- {k}: {v}" for k, v in facts.items() if v not in (None, ""))
    if medium == "email":
        envelope = (
            "Write it as a short email in your voice: open with a brief greeting, and make the "
            "ask clearly. Use *italics* for book titles and **bold** sparingly on the key facts "
            "(the date, who's confirmed). Do not sign off — a signature is added automatically. "
            "No subject line and no markdown headings."
        )
    else:
        envelope = (
            "Write it as a short Discord message in your voice: no greeting, no sign-off, no "
            "markdown headings, no bulleted lists."
        )
    prompt = (
        f"Compose a {kind} from these exact facts. Use the names, numbers, and dates exactly "
        f"as given — do not invent, drop, or change any of them. {envelope} Output only the "
        "finished message — no preamble, no notes to me, no '---' dividers, nothing before or "
        f"after it.\n\n"
        f"Facts:\n{facts_lines}"
    )
    try:
        resp = _get_client().messages.create(
            model=MODEL,
            max_tokens=COMPOSE_MAX_TOKENS,
            system=_system_blocks(),
            messages=[{"role": "user", "content": prompt}],
        )
        return _text_of(resp.content).strip() or fallback
    except Exception:  # noqa: BLE001 — proactive copy must degrade to its template
        log.warning("compose(%s) failed; using fallback template", kind, exc_info=True)
        return fallback


def complete(system: str, user: str, *, model: str = OPUS_MODEL, max_tokens: int = 4096,
             effort: str | None = "medium", thinking: bool = True, timeout: float = 600.0) -> str:
    """A raw, tool-less, stateless completion against a caller-supplied system prompt.

    Neither `compose` (Sonnet, 400-token cap, charter system prompt) nor `generate` (full tool
    loop) fits an offline batch extractor like the archive miner, which needs its own system
    prompt and no tools. This is that primitive: one prompt in, text out, on Opus by default.

    `thinking=False` omits adaptive thinking (mechanical extraction needs none — cheaper/faster,
    and keeps the request valid on models that don't take the thinking/effort params); `effort`
    is only sent when truthy. Synchronous; call via asyncio.to_thread. Raises on API error (the
    caller decides how to degrade) — unlike compose, there is no fallback here.
    """
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if thinking:
        kwargs["thinking"] = {"type": "adaptive"}
        if effort:
            kwargs["output_config"] = {"effort": effort}
    resp = _get_client().with_options(timeout=timeout).messages.create(**kwargs)
    return _text_of(resp.content).strip()


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
    # A bounded extraction, so the simple call shape is sufficient — no thinking
    # or output_config needed even though SUMMARY_MODEL is now Sonnet.
    resp = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    # Log the summary call too, so the cost report sees its spend (rounds=0
    # marks it as the internal summary path, not a user-facing agent turn).
    u = resp.usage
    db.log_usage(channel_id, SUMMARY_MODEL,
                 input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                 cache_read=u.cache_read_input_tokens or 0,
                 cache_creation=u.cache_creation_input_tokens or 0, rounds=0)
    new_summary = _text_of(resp.content)
    if new_summary:
        db.set_summary(channel_id, new_summary, to_fold[-1]["id"])
