"""Club-wide meeting-cadence emails: the 1-week reminder and the 2-day topic email.

Both go to the whole mailing list at their cadence window (wired in run_scheduler).
The 1-week reminder just voices attendance facts via oliver.compose; the 2-day topic
email runs Oliver's tool loop (oliver.generate) so he mines the club's reading history
for real, non-obvious discussion provocations. Each closes with the contextual signature.

Build-time preview / test:
    python -m agent.club.meeting_emails topic            # print the draft
    python -m agent.club.meeting_emails topic --send a@b  # also deliver it to that address
"""

from __future__ import annotations

import argparse
import json
import re

from agent import config
from agent import corpus_read as cr
from agent import db, oliver
from agent.club import meeting_rules
from agent.club.meeting_rules import friendly_date as _friendly_date
from agent.mail import outbound

_EMAIL_TAG = re.compile(r"<email>(.*?)</email>", re.S | re.I)
_CITE_TAG = re.compile(r"</?cite[^>]*>", re.I)


def _strip_cite_tags(text: str) -> str:
    """web_search grounds cited spans in `<cite index=…>…</cite>` markers; keep the prose but drop
    the markup so it never lands in a member's inbox (research emails: topic brief + Postscript)."""
    return _CITE_TAG.sub("", text)


def _extract_email(text: str) -> str:
    """Pull the email out of <email>…</email>, tolerating a missing closing tag, and drop
    any tool-loop preamble/notes and web_search <cite> markers around it."""
    match = _EMAIL_TAG.search(text)
    if match:
        return _strip_cite_tags(match.group(1).strip())
    # Unclosed/partial tags (e.g. truncated generation): take everything after an opening
    # <email>, then strip any stray tag so it never leaks into the sent email.
    opened = re.search(r"<email>", text, re.I)
    if opened:
        text = text[opened.end():]
    return _strip_cite_tags(re.sub(r"</?email\s*>", "", text, flags=re.I).strip())


def topic_email_prompt(meeting: dict) -> str:
    book = meeting.get("book") or {}
    title = book.get("title") or "our next book"
    authors = ", ".join(book.get("authors") or []) or "the author"
    when = _friendly_date(meeting.get("date")) or "the meeting date"
    pickers = ", ".join(meeting.get("pickerNames") or [])
    picker_line = f" {pickers} picked it." if pickers else ""
    return (
        "Write the club's pre-meeting email that goes out two days before we meet, to the whole "
        "mailing list. Research it properly with your tools first, then write a rich, surprising "
        "email a long-time member would be glad to get.\n\n"
        f"Open with a short greeting and a one-line reminder that we meet {when} (it's two days "
        f"out — phrase the date the way a person would, like 'this Tuesday', not a numeric date) "
        f"to discuss {title} by {authors}.{picker_line}\n\n"
        "Then a second paragraph (2-4 sentences) that sets the book up and bridges into what "
        "follows — why it's worth the evening, the tension or question at its heart, and a nod "
        "that what's below is material to chew on before Tuesday. Make it inviting, not a "
        "summary.\n\n"
        "Then three sections, each under its own '## ' header, in this exact order:\n\n"
        "## On the Book\n"
        "5-7 discussion questions about THIS book on its own terms — its arguments, methods, the "
        "author's choices, what's provocative, weak, or unresolved in it. These must NOT reference "
        "other club books; they're for digging into this one. Write them as a numbered list so we "
        "can refer to them by number in the meeting.\n\n"
        "## Connections\n"
        "3-5 provocations, as a numbered list, that set this book against OUR OWN reading history — "
        "real, specific, non-obvious connections to past club books, recurring arguments we've had, "
        "author or topic patterns, questions we've never settled. Ground these in what the club has "
        "actually read and discussed; use related_books, compare_books, review_summary, and "
        "search_mail_archive.\n\n"
        "## A third section — your call\n"
        "Give it your own header and surprise us: something genuinely interesting and unexpected "
        "about the book, its author, its making, its reception, a strange fact, or a debate it "
        "sparked. Use web_search if it helps you find a real, delightful angle this "
        "technically-minded club hasn't heard. Make it the part people forward to a friend.\n\n"
        "Format: this renders as an HTML email, so use markdown — a '## ' header for each section, "
        "numbered lists for the questions, *italics* for book titles, and **bold** sparingly on a "
        "key phrase or two per section so it's easy to skim. Separate every paragraph with a fully "
        "blank line, and leave a blank line after each '## ' header — never run paragraphs together "
        "on consecutive lines (especially in the third section, which is prose). Do NOT use '---' "
        "or horizontal-rule lines anywhere — the section headers carry the structure. Write the "
        "ENTIRE email between <email> and </email> tags and put NOTHING outside them — no preamble, "
        "no notes, no sign-off (a signature is added automatically)."
    )


def topic_email(meeting: dict | None = None) -> dict:
    """The 2-day discussion-topics email. Subject + body (body includes the signature)."""
    meeting = meeting or meeting_rules.next_meeting()
    title = (meeting.get("book") or {}).get("title") or "our next book"
    when = _friendly_date(meeting.get("date"))
    body = _extract_email(oliver.generate(topic_email_prompt(meeting)))  # signature added by outbound.send
    subject = f"Discussion topics for {title}" + (f" — {when}" if when else "")
    return {"subject": subject, "body": body}


# ── Postscript: the ~1-week-after-meeting digest of what's new with our books ──
POSTSCRIPT_KIND = "postscript_sent"          # group-event kind (dedup + rotation store)
POSTSCRIPT_SEARCH_BUDGET = 15                 # raised web_search cap (default 3 is far too few)
POSTSCRIPT_POOL_SIZE = 15                     # candidates fed to Oliver (spanning eras)
_MAX_PER_AUTHOR = 2                           # cap repeats so the pool isn't clustered


def _most_recent_read_book() -> dict | None:
    read = [b for b in cr.books() if b.get("isRead") and b.get("meetingDate")]
    return max(read, key=lambda b: b["meetingDate"]) if read else None


def _recent_featured_slugs(limit: int = 3) -> set[str]:
    """Book slugs featured (offered) in the last few Postscripts, so editions rotate not repeat."""
    out: set[str] = set()
    for detail in db.recent_group_event_details(POSTSCRIPT_KIND, limit=limit):
        if not detail:
            continue
        try:
            out.update(json.loads(detail).get("featured") or [])
        except (ValueError, TypeError):
            continue
    return out


def _candidate_facts(book: dict, author: dict | None) -> dict:
    """Targeting facts for one candidate so Oliver's news search is specific — and ready link
    targets (our own book page + an external reference) so it links rather than inventing URLs."""
    slug = book.get("slug")
    external = (f"https://openlibrary.org{book['olKey']}" if book.get("olKey")
                else book.get("wikipediaUrl"))
    return {
        "slug": slug,
        "title": book.get("title"),
        "authors": book.get("authors") or [],
        "topic": book.get("topic"),
        "yearRead": (book.get("meetingDate") or "")[:4] or None,
        "picker": book.get("pickerName"),
        "fiction": bool(book.get("fiction")),
        "clubUrl": f"{config.SITE_URL}/books/{slug}/" if slug else None,
        "externalUrl": external,
        "authorWebsite": (author or {}).get("website"),
        "authorNotableWorks": (author or {}).get("notableWorks"),
    }


def _news_score(book: dict, author: dict | None) -> int:
    """How likely a book is to have real current news: fiction (adaptations) and living, active
    authors (a website / notable works, no death year) score higher."""
    s = 2 if book.get("fiction") else 0
    if author:
        if not author.get("deathYear"):
            s += 5
        if author.get("website") or author.get("notableWorks"):
            s += 3
    return s


def select_postscript_candidates(anchor_slug: str | None, *, exclude: set[str] | None = None,
                                  limit: int = POSTSCRIPT_POOL_SIZE) -> list[dict]:
    """A temporally varied, author-diverse pool of read books likely to have real news. Read books
    are split into Distant / Middle / Recent thirds by meeting date and the top-scored few are drawn
    from EACH era, so an edition spans the club's history rather than clustering on recent reads; the
    anchor (just-discussed book) always leads. Repeats per author are capped so it isn't one-note.
    Returns candidate-fact dicts (with link targets) for the prompt."""
    exclude = exclude or set()
    read = [b for b in cr.books() if b.get("isRead") and b.get("slug") and b.get("meetingDate")]
    by_slug = {b["slug"]: b for b in read}
    eligible = sorted((b for b in read if b["slug"] not in exclude and b["slug"] != anchor_slug),
                      key=lambda b: b["meetingDate"])  # oldest → newest
    n = len(eligible)
    third = max(1, n // 3)
    eras = [eligible[:third], eligible[third:2 * third], eligible[2 * third:]] if n else []
    per_era = max(1, limit // 3)

    _author_cache: dict[str, dict | None] = {}

    def author_of(book: dict) -> dict | None:
        if not book.get("authors"):
            return None
        name = book["authors"][0]
        if name not in _author_cache:
            _author_cache[name] = cr.get_author(name)
        return _author_cache[name]

    picks: list[dict] = []
    author_count: dict[str, int] = {}

    def take(book: dict) -> None:
        picks.append(_candidate_facts(book, author_of(book)))
        for a in book.get("authors") or []:
            author_count[a] = author_count.get(a, 0) + 1

    if anchor_slug and anchor_slug in by_slug:  # the just-discussed book leads
        take(by_slug[anchor_slug])

    for era in eras:
        # Cheap prerank (fiction, then newest within the era) bounds get_author to a shortlist;
        # then rank that shortlist by newsworthiness and take the top few, capping per author.
        shortlist = sorted(era, key=lambda b: (b.get("fiction", False), b.get("meetingDate") or ""),
                           reverse=True)[:per_era * 3]
        ranked = sorted(shortlist, key=lambda b: _news_score(b, author_of(b)), reverse=True)
        added = 0
        for b in ranked:
            if added >= per_era or len(picks) >= limit:
                break
            if any(author_count.get(a, 0) >= _MAX_PER_AUTHOR for a in (b.get("authors") or [])):
                continue
            take(b)
            added += 1
    return picks


def postscript_prompt(candidates: list[dict], *, anchor_title: str | None = None) -> str:
    rows = []
    for c in candidates:
        auth = ", ".join(c["authors"]) or "the author"
        read = f"read {c['yearRead']}" if c.get("yearRead") else "read"
        pick = f", {c['picker']}'s pick" if c.get("picker") else ""
        links = []
        if c.get("clubUrl"):
            links.append(f"our page {c['clubUrl']}")
        if c.get("externalUrl"):
            links.append(f"ref {c['externalUrl']}")
        if c.get("authorWebsite"):
            links.append(f"author site {c['authorWebsite']}")
        if c.get("authorNotableWorks"):
            links.append("other works: " + ", ".join(c["authorNotableWorks"][:4]))
        tail = f" — {'; '.join(links)}" if links else ""
        rows.append(f"- *{c['title']}* by {auth} ({read}{pick}){tail}")
    candidate_block = "\n".join(rows)
    anchor_line = (
        f"Lead with a follow-up on *{anchor_title}* (the book we just discussed) IF you find "
        "something genuinely new about it; otherwise skip straight to the rest.\n\n"
        if anchor_title else "")
    return (
        "Write 'Postscript' — the club's after-meeting email, sent about a week after we meet, to "
        "the whole mailing list. It's a warm, magazine-y digest of what's genuinely NEW or newly "
        "resonant in the world with the books and authors we've read. Research with web_search "
        "FIRST, then write only what you actually found.\n\n"
        "WHAT MAKES A GOOD ITEM (mix these — an edition that's five of one kind is a failure):\n"
        "- a film/TV/stage adaptation, in the works or newly out;\n"
        "- a new or forthcoming book from an author we've read;\n"
        "- a major award or honor, or (sadly) an author's passing;\n"
        "- a book of ours suddenly back in the cultural conversation;\n"
        "- a TANGENT: a meme, a live debate, an essay, or a current event that genuinely rhymes "
        "with something we read ('the thing everyone's arguing about right now is exactly what "
        "*[book]* was about'). For a tangent the external thing must be REAL and sourced; the "
        "CONNECTION is your own read — frame it as your insight ('this reminds me of…'), not as a "
        "fact, and never invent the external thing.\n\n"
        "DIG UNTIL IT'S GOOD — don't just take the first few candidates:\n"
        "- Work the WHOLE list below and keep researching until you have about 5-6 genuinely "
        "notable items. If the recent titles are dry, dig into the middle and older ones — they're "
        "grouped across our whole history on purpose.\n"
        "- Make it VARIED: span different eras, topics, and authors, and mix the KINDS of items "
        "above. Older reads with a fresh tangent are often the most delightful entries.\n\n"
        "HARD RULES — grounding is everything; one made-up item destroys the whole thing:\n"
        "- Every factual item MUST come from a real web_search result you can attribute. If you're "
        "not sure it's true and current, LEAVE IT OUT.\n"
        "- NEVER invent an adaptation, award, or release, and don't hedge with 'reportedly' to "
        "smuggle in something unverified. If a candidate turns up nothing, DROP it silently.\n"
        "- Put every item in your OWN words; never paste jacket copy or press-release language.\n\n"
        "LINK GENEROUSLY so members can explore — use markdown links, NOT <cite> tags:\n"
        "- Give each item a source link — [where you found it](the real URL from your search).\n"
        "- Link each book we've read to its club page via the 'our page' URL below, e.g. "
        "[*Title*](our-page-url); add an author-site or reference ('ref') link where it helps.\n\n"
        "Candidates (books the club has read, drawn across our whole history — search each for "
        "recent news and search the author for new/forthcoming writing; the links help you aim and "
        "cite):\n"
        f"{candidate_block}\n\n"
        f"{anchor_line}"
        "Open with a short warm greeting, then the items as the spine of the email. Give each item "
        "its own '## ' header (the book or author) and 2-4 sentences: what's new or resonant (your "
        "words, with links), and one line tying it to us — when we read it and who picked it, using "
        "ONLY the read-year and picker facts above (never guess those).\n\n"
        "Format: renders as an HTML email — markdown, a '## ' header per item, *italics* for titles, "
        "**bold** sparingly, and markdown [links](url). Blank line between every paragraph and after "
        "each header. Do NOT use '---' or horizontal rules. Write the ENTIRE email between <email> "
        "and </email> tags with NOTHING outside them — no preamble, no sign-off (a signature is "
        "added automatically)."
    )


def postscript_email(anchor: dict | None = None) -> dict:
    """The after-meeting 'Postscript' digest. Returns subject, body (signature added on send), and
    the `offered` candidate slugs (the scheduler records these so future editions rotate)."""
    anchor = anchor if anchor is not None else (_most_recent_read_book() or {})
    candidates = select_postscript_candidates(anchor.get("slug"), exclude=_recent_featured_slugs())
    body = _extract_email(oliver.generate(
        postscript_prompt(candidates, anchor_title=anchor.get("title")),
        web_search_max_uses=POSTSCRIPT_SEARCH_BUDGET))
    return {"subject": "Postscript: what's new with our books", "body": body,
            "offered": [c["slug"] for c in candidates if c.get("slug")]}


def _names(status: dict, *want: str) -> list[str]:
    return [r["member"] for r in status.get("attendance", []) if r["status"] in want and r.get("member")]


def week_reminder(meeting: dict | None = None, status: dict | None = None) -> dict:
    """The 1-week reminder: meeting + the full attendance picture. Subject + body (with signature)."""
    meeting = meeting or meeting_rules.next_meeting()
    status = status or meeting_rules.meeting_status(meeting["meetingKey"])
    title = (meeting.get("book") or {}).get("title") or "our next book"
    when = _friendly_date(meeting.get("date"))
    coming = _names(status, "yes")
    not_coming = _names(status, "no")
    waiting_on = _names(status, "pending", "unsure")
    fallback = (
        f"Hi all — a reminder that the R/W Book Club meets {when} to discuss {title}.\n\n"
        + (f"Coming: {', '.join(coming)}.\n" if coming else "")
        + (f"Can't make it: {', '.join(not_coming)}.\n" if not_coming else "")
        + (f"\nStill need to hear from {', '.join(waiting_on)} — please reply.\n"
           if waiting_on else "\nThat's everyone — thanks for the quick replies.\n")
    )
    body = oliver.compose(
        "one-week-out reminder email to the whole club mailing list",
        {
            "occasion": "the monthly meeting is about a week away",
            "book": title,
            "meeting date": f"{when} (about a week out — say it naturally, like 'next Tuesday')",
            "confirmed coming": ", ".join(coming) or "no one yet",
            "not able to make it": ", ".join(not_coming) or None,
            "still waiting to hear from": ", ".join(waiting_on) or "everyone has responded",
            "ask": "warmly nudge ONLY the people we're still waiting to hear from; never ask "
                   "someone who already said yes or no to respond again",
        },
        fallback=fallback,
        medium="email",
    )  # signature added by outbound.send
    subject = f"Reminder: {title} — {when}" if when else f"Reminder: {title}"
    return {"subject": subject, "body": body}


def _main() -> None:
    parser = argparse.ArgumentParser(description="Preview (and optionally send) a club meeting email.")
    parser.add_argument("kind", choices=["topic", "week", "postscript"])
    parser.add_argument("--send", metavar="EMAIL", help="also deliver the draft to this address")
    args = parser.parse_args()

    if args.kind == "postscript":
        # Post-meeting digest — works off PAST reads, so no upcoming meeting required.
        email = postscript_email()
    else:
        meeting = meeting_rules.next_meeting()
        if not meeting or not (meeting.get("book") or {}).get("title"):
            print("No upcoming meeting scheduled — nothing to generate.")
            return
        email = topic_email(meeting) if args.kind == "topic" else week_reminder(meeting)

    print(f"Subject: {email['subject']}\n")
    print(outbound.finalize(email["body"]))  # show exactly what would be sent (incl. signature)

    if args.send:
        outbound.send(to=[args.send], subject=email["subject"], body=email["body"])
        print(f"\n[sent to {args.send}]")


if __name__ == "__main__":
    _main()
