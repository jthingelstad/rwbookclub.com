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
import re

from agent import oliver
from agent.club import meeting_rules
from agent.club.meeting_rules import friendly_date as _friendly_date
from agent.mail import outbound

_EMAIL_TAG = re.compile(r"<email>(.*?)</email>", re.S | re.I)


def _extract_email(text: str) -> str:
    """Pull the email out of <email>…</email>, tolerating a missing closing tag, and drop
    any tool-loop preamble/notes around it."""
    match = _EMAIL_TAG.search(text)
    if match:
        return match.group(1).strip()
    # Unclosed/partial tags (e.g. truncated generation): take everything after an opening
    # <email>, then strip any stray tag so it never leaks into the sent email.
    opened = re.search(r"<email>", text, re.I)
    if opened:
        text = text[opened.end():]
    return re.sub(r"</?email\s*>", "", text, flags=re.I).strip()


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
    parser.add_argument("kind", choices=["topic", "week"])
    parser.add_argument("--send", metavar="EMAIL", help="also deliver the draft to this address")
    args = parser.parse_args()

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
