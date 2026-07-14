"""Review drive: Oliver collects written reviews by email.

The club record has ratings but almost no written reviews. Once a week (Wednesday morning,
club time), Oliver emails each allowlisted member about ONE book they rated but never wrote
up: "just reply in your own words." The reply comes back through a small state machine:

    awaiting_reply --(member email)--> extract --> confirmation email --> awaiting_confirm
    awaiting_confirm --YES--> reviews.write_review + publish + thanks --> written
    awaiting_confirm --corrections--> re-extract --> confirmation (max 2 rounds) --> parked
    any state --"stop asking"--> review_optout event + durable memory --> declined

Safety contract (the whole point):
- The review BODY is the member's own sentences, lightly trimmed of greetings/sign-offs —
  never paraphrased, never generated.
- Numeric fields (rating/discussion) are captured ONLY when explicitly stated; never inferred
  from tone. Absent → left empty; a body-only review is a fine review.
- Nothing is written to the club record without the member's explicit YES.

Gating: config.REVIEW_DRIVE_MEMBERS (env OLIVER_REVIEW_DRIVE_MEMBERS) — a member-slug
allowlist; "all" opens it to every current member; empty disables the feature. Cadence and
caps below are product decisions, not knobs (meeting_campaign precedent).
"""

from __future__ import annotations

import json
import logging
import re

from agent import clubdb, config, db, oliver
from agent import corpus_read as cr
from agent.club import meeting_rules, reviews
from agent.mail import email_policy, outbound

log = logging.getLogger("oliver.review_drive")

ASK_WEEKDAY = 2          # Wednesday
ASK_HOUR = 10            # 10am club time
MAX_ASKS_PER_BOOK = 2    # then never ask about that book again
ASK_COOLDOWN_DAYS = 7    # "at most once a week" per member
MAX_CORRECTION_ROUNDS = 2  # then park and point at the web app
ASK_EXPIRY_DAYS = 21       # an unanswered ask expires; the member is free for future asks

JOB_KEY = "review_drive"
YES_RE = re.compile(r"^\s*(yes|yep|yeah|yup|looks good|perfect|that works|correct|👍|do it)\b",
                    re.IGNORECASE)
NO_RE = re.compile(r"^\s*(no thanks|no\b|nope|skip|pass|not now)", re.IGNORECASE)

_EXTRACT_SYSTEM = (
    "You convert a book-club member's emailed book review into structured fields. Their words "
    "are sacred: `body` is their own sentences, lightly trimmed of greetings, sign-offs, and "
    "email pleasantries — NEVER paraphrased, NEVER summarized, NEVER improved.\n\n"
    "Numeric fields are captured ONLY when explicitly stated: 'four stars' or '4/5' → rating 4; "
    "'8 out of 10' → rating 4 (halve a stated 10-scale, round down); 'DNF'/'couldn't finish' → "
    "rating \"DNF\". A stated discussion quality (1-5) likewise. If a number is not explicitly "
    "stated, the field is null — NEVER infer a rating from tone.\n"
    "`recommend` true/false only on an explicit statement ('I'd recommend it'); else null. "
    "`quote` only if they call out a favorite quote/passage; else null.\n"
    "`declined` true if they're saying they don't want to review THIS book. `stop_asking` true "
    "if they don't want review-request emails at all.\n"
    "If a prior draft is provided, the new text is CORRECTIONS: apply them to the draft (their "
    "correction words win) and return the full updated result.\n\n"
    "OUTPUT strict JSON only, no prose, no code fences:\n"
    '{"body": "...", "rating": 4, "recommend": null, "discussion": null, "quote": null, '
    '"declined": false, "stop_asking": false}'
)


def allowlisted_slugs() -> set[str]:
    raw = (config.REVIEW_DRIVE_MEMBERS or "").strip()
    if not raw:
        return set()
    if raw.lower() == "all":
        return {m["slug"] for m in cr.human_current_members()}
    return {s.strip() for s in raw.split(",") if s.strip()} & {
        m["slug"] for m in cr.human_current_members()}


def _ask_counts(conn, member_id: int) -> tuple[dict[str, int], str | None]:
    """(per-book ask counts, most recent ask timestamp) from the review_requested ledger."""
    rows = conn.execute(
        "SELECT detail, occurred_at FROM events WHERE kind = 'review_requested' "
        "AND member_id = ? ORDER BY occurred_at DESC", (member_id,)).fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        try:
            slug = json.loads(r["detail"] or "{}").get("book_slug")
        except (ValueError, TypeError):
            slug = None
        if slug:
            counts[slug] = counts.get(slug, 0) + 1
    return counts, (rows[0]["occurred_at"] if rows else None)


def _opted_out(conn, member_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM events WHERE kind = 'review_optout' AND member_id = ? LIMIT 1",
        (member_id,)).fetchone() is not None


def next_candidate(slug: str) -> dict | None:
    """The best book to ask `slug` about, or None. Rated (not DNF) but never written up,
    best-rated first then most recently read; honors the per-book ask cap, the weekly
    cooldown, opt-out, and any in-flight draft."""
    member_id = clubdb.lookup_member_id(slug)
    if member_id is None or db.open_draft_for_member(member_id):
        return None
    with db.connect() as conn:
        if _opted_out(conn, member_id):
            return None
        counts, last_ask = _ask_counts(conn, member_id)
        if last_ask and conn.execute(
                "SELECT 1 WHERE ? > datetime('now', ?)",
                (last_ask, f"-{ASK_COOLDOWN_DAYS} days")).fetchone():
            return None
        rows = conn.execute(
            "SELECT b.slug, b.title, r.rating, MAX(m.date) AS read_date "
            "FROM club_reviews r "
            "JOIN club_books b ON b.id = r.book_id "
            "JOIN club_members mem ON mem.id = r.member_id "
            "LEFT JOIN club_meeting_books mb ON mb.book_id = b.id "
            "LEFT JOIN club_meetings m ON m.id = mb.meeting_id "
            "WHERE mem.slug = ? AND r.rating IS NOT NULL AND COALESCE(r.dnf, 0) = 0 "
            "AND COALESCE(r.body, '') = '' "
            "GROUP BY b.id "
            # never ask about a book the club read before this member joined (fail open on NULLs)
            "HAVING (mem.joined IS NULL OR MAX(m.date) IS NULL OR MAX(m.date) >= mem.joined) "
            "ORDER BY r.rating DESC, read_date DESC", (slug,)).fetchall()
    for r in rows:
        if counts.get(r["slug"], 0) < MAX_ASKS_PER_BOOK:
            return {"slug": r["slug"], "title": r["title"], "rating": r["rating"],
                    "readDate": r["read_date"], "memberId": member_id}
    return None


def send_ask(slug: str, candidate: dict | None = None) -> dict | None:
    """Send one review ask to `slug` (candidate auto-selected if not given). Creates the
    draft row + the review_requested ledger event. Returns {book, threadId} or None."""
    member = cr.find_member(slug)
    rec = db.email_for_member(slug)
    cand = candidate or next_candidate(slug)
    if not member or not rec or not cand:
        return None
    first = (member.get("name") or slug).split()[0]
    when = meeting_rules.friendly_date(cand.get("readDate")) if cand.get("readDate") else None
    body = oliver.compose(
        "a short, warm email asking this member to write a few sentences of review for one "
        "book they rated but never reviewed — reference their own rating and when the club "
        "read it, and make clear they can just REPLY to this email in their own words; a few "
        "sentences is a great review, and you'll handle recording it",
        {"member": first, "book": cand["title"], "theirRating": cand["rating"],
         "whenRead": when or "a while back"},
        fallback=(f"Hi {first} — you gave *{cand['title']}* {cand['rating']} stars"
                  + (f" back when we read it ({when})" if when else "") +
                  ", but the club record has no written review from you. Would you write one? "
                  "Just reply to this email in your own words — a few sentences is a great "
                  "review, and I'll take care of recording it."))
    sent = outbound.send(to=[rec["email"]], subject=f"Your review of {cand['title']}?", body=body)
    # The rating is already an explicit, canonical member statement from the web app. Seed it
    # into the draft so the extraction turn cannot forget it merely because the member replies
    # with prose rather than repeating a number Oliver just quoted back to them.
    initial = {
        "body": "", "rating": cand["rating"], "recommend": None,
        "discussion": None, "quote": None,
    }
    draft_id = db.create_review_draft(member_id=cand["memberId"], book_slug=cand["slug"],
                                      thread_id=sent.get("threadId"),
                                      draft_json=json.dumps(initial))
    db.record_event(actor="oliver", kind="review_requested", member_id=cand["memberId"],
                    surface="email", category="reading",
                    detail=json.dumps({"book_slug": cand["slug"],
                                       "thread_id": sent.get("threadId"),
                                       "draft_id": draft_id}))
    db.add_activity("review_drive", "Review requested",
                    f"Asked {slug} for a review of {cand['title']}.")
    log.info("review ask sent: %s -> %s (%s)", cand["title"], slug, sent.get("threadId"))
    return {"book": cand["title"], "threadId": sent.get("threadId")}


def run(now) -> int:
    """Weekly gate + one ask per eligible allowlisted member. Called from the hourly tick."""
    slugs = allowlisted_slugs()
    if not slugs:
        return 0
    if now.weekday() != ASK_WEEKDAY or now.hour != ASK_HOUR:
        return 0
    week = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
    state = db.get_job_state(JOB_KEY) or {}
    if state.get("week") == week:
        return 0
    expired = db.expire_stale_review_drafts(ASK_EXPIRY_DAYS)
    if expired:
        db.add_activity("review_drive", "Review drafts expired",
                        f"{expired} unanswered ask(s) older than {ASK_EXPIRY_DAYS} days released.")
    sent = 0
    for slug in sorted(slugs):
        try:
            if send_ask(slug):
                sent += 1
        except Exception:  # noqa: BLE001 — one member's failure must not block the rest
            log.exception("review ask failed for %s", slug)
    db.set_job_state(JOB_KEY, {"week": week, "sent": sent})
    return sent


# ── Reply handling ───────────────────────────────────────────────────────────
def _extract(book_title: str, text: str, prior: dict | None) -> dict | None:
    """Strict-JSON extraction (one retry). None = unparseable twice."""
    user = f"Book under review: {book_title}\n\n"
    if prior:
        user += f"PRIOR DRAFT (the new text below corrects it):\n{json.dumps(prior)}\n\n"
    user += f"Member's email:\n{text}"
    for _ in range(2):
        raw = oliver.complete(_EXTRACT_SYSTEM, user, model=oliver.MODEL, max_tokens=16000,
                              usage_channel="review_drive")
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                out = json.loads(m.group(0))
                if isinstance(out.get("body"), str):
                    return out
            except ValueError:
                pass
    return None


def _confirmation_body(first: str, book_title: str, d: dict) -> str:
    rating = d.get("rating")
    stars = ("DNF" if rating == "DNF"
             else "★" * int(rating) + "☆" * (5 - int(rating)) if rating else "(no rating stated)")
    bits = [f"Hi {first} — here's how I'll record your review of *{book_title}*:", "",
            f"- **Rating**: {stars}"]
    if d.get("recommend") is not None:
        bits.append(f"- **Would recommend**: {'yes' if d['recommend'] else 'no'}")
    if d.get("discussion"):
        bits.append(f"- **Discussion quality**: {d['discussion']}/5")
    if d.get("quote"):
        bits.append(f"- **Favorite quote**: “{d['quote']}”")
    bits += ["", "> " + (d.get("body") or "").replace("\n", "\n> "), "",
             "Reply **YES** and it's recorded (your words, verbatim) — or just tell me what "
             "to change."]
    return "\n".join(bits)


def _write(draft: dict, d: dict) -> dict:
    member_name = next((m["name"] for m in cr.members()
                        if clubdb.lookup_member_id(m["slug"]) == draft["member_id"]), None)
    # Review-drive candidates already have a structured review row (at least a rating). A prose
    # reply may omit those fields, which means "unchanged", not "clear them". Read the canonical
    # row as a final deterministic guard even for drafts created before rating seeding shipped.
    with db.connect() as conn:
        existing = conn.execute(
            "SELECT r.rating, r.dnf, r.discussion_quality, r.would_recommend, "
            "r.favorite_quote, r.body FROM club_reviews r "
            "JOIN club_books b ON b.id = r.book_id "
            "WHERE r.member_id = ? AND b.slug = ?",
            (draft["member_id"], draft["book_slug"]),
        ).fetchone()
    existing = dict(existing) if existing else {}
    rating = d.get("rating")
    if rating is None:
        rating = "DNF" if existing.get("dnf") else existing.get("rating")
    recommend = d.get("recommend")
    if recommend is None and existing:
        recommend = bool(existing.get("would_recommend"))
    discussion = d.get("discussion")
    if discussion is None:
        discussion = existing.get("discussion_quality")
    quote = d.get("quote")
    if quote is None:
        quote = existing.get("favorite_quote")
    body = d.get("body") or existing.get("body")
    return reviews.write_review(
        draft["book_slug"], member_name,
        rating=str(rating) if rating else None,
        review=body or None,
        recommend="yes" if recommend else None,
        discussion=str(discussion) if discussion else None,
        quote=quote or None)


def handle_reply(draft: dict, msg) -> bool:
    """Handle one inbound review email.

    Returns True only when a confirmed review write needs a site publish. The caller owns
    scheduling that publish because this function runs in a worker thread and the publisher must
    be created on the bot's asyncio event loop.
    """

    member = next((m for m in cr.members()
                   if clubdb.lookup_member_id(m["slug"]) == draft["member_id"]), None)
    first = ((member or {}).get("name") or "there").split()[0]
    book = cr.find_book(draft["book_slug"]) or {"title": draft["book_slug"]}
    text = email_policy.current_message_text(getattr(msg, "text", "") or "")
    reply_kw = dict(to=[msg.from_email], subject=f"Re: {msg.subject}",
                    in_reply_to=msg.message_id, references=msg.references)

    def _finish(state: str, note: str) -> None:
        db.update_review_draft(draft["id"], state=state)
        db.add_activity("review_drive", f"Review draft {state}",
                        f"{(member or {}).get('slug', draft['member_id'])} / {book['title']}: {note}")

    if draft["state"] == "awaiting_confirm" and YES_RE.search(text):
        d = json.loads(draft["draft_json"] or "{}")
        result = _write(draft, d)
        outbound.send(body=oliver.compose(
            "a one-or-two sentence warm thanks: their review is recorded and will be on the "
            "club site shortly", {"member": first, "book": book["title"]},
            fallback=f"Recorded — thanks, {first}! Your review of *{book['title']}* will be on "
                     "the club site shortly."), **reply_kw)
        db.record_event(actor="member", kind="review_recorded", member_id=draft["member_id"],
                        surface="email", category="reading",
                        detail=json.dumps({"book_slug": draft["book_slug"]}))
        _finish("written", f"review written ({result.get('rating') or 'no rating'})")
        return True

    d0 = json.loads(draft["draft_json"] or "null")
    if draft["state"] == "awaiting_confirm" and NO_RE.search(text) and len(text) < 80:
        outbound.send(body=f"No problem, {first} — I'll leave it be.", **reply_kw)
        _finish("declined", "member declined at confirmation")
        return False

    d = _extract(book["title"], text, d0)
    if d is None:
        db.add_activity("warning", "Review extraction failed",
                        f"{book['title']}: unparseable extraction twice; parked.")
        outbound.send(body=(f"I had trouble turning that into the record cleanly, {first} — "
                            "mind finishing it in the web app? `/oliver my-club` in Discord "
                            "gets you a link, and your email is safe with me meanwhile."),
                      **reply_kw)
        _finish("parked", "extraction unparseable")
        return False

    # Missing structured fields mean the member did not change the prior canonical values. Keep
    # those values deterministically instead of relying on the extractor to reproduce them.
    if d0:
        for field in ("rating", "recommend", "discussion", "quote"):
            if d.get(field) is None and d0.get(field) is not None:
                d[field] = d0[field]

    if d.get("stop_asking"):
        db.record_event(actor="member", kind="review_optout", member_id=draft["member_id"],
                        surface="email", category="reading")
        db.add_memory("Prefers not to receive review-request emails",
                      scope="member", subject=(member or {}).get("slug"),
                      source="member_request")
        outbound.send(body=f"Understood, {first} — no more review emails from me. "
                           "The web app's always there if the mood ever strikes.", **reply_kw)
        _finish("declined", "member opted out of review emails")
        return False

    if d.get("declined"):
        outbound.send(body=f"Fair enough, {first} — skipping that one.", **reply_kw)
        _finish("declined", "member declined this book")
        return False

    rounds = draft["rounds"] + (1 if draft["state"] == "awaiting_confirm" else 0)
    if rounds > MAX_CORRECTION_ROUNDS:
        outbound.send(body=(f"We're going in circles by email, {first} — easier hands-on: "
                            "`/oliver my-club` in Discord opens the web app and your words so "
                            "far are saved in this thread."), **reply_kw)
        _finish("parked", "correction rounds exhausted")
        return False

    db.update_review_draft(draft["id"], state="awaiting_confirm",
                           draft_json=json.dumps(d), rounds=rounds)
    outbound.send(body=_confirmation_body(first, book["title"], d), **reply_kw)
    db.add_activity("review_drive", "Review draft awaiting confirmation",
                    f"{(member or {}).get('slug', '?')} / {book['title']} (round {rounds})")
    return False
