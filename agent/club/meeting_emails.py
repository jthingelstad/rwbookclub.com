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
from agent.mail import outbound

_EMAIL_TAG = re.compile(r"<email>(.*?)</email>", re.S | re.I)


def _extract_email(text: str) -> str:
    """Pull the email out of <email>…</email>, dropping any tool-loop preamble/notes."""
    match = _EMAIL_TAG.search(text)
    return (match.group(1) if match else text).strip()


def _committed_names(status: dict) -> list[str]:
    return [r["member"] for r in status.get("attendance", []) if r["status"] == "yes" and r.get("member")]


def topic_email_prompt(meeting: dict) -> str:
    book = meeting.get("book") or {}
    title = book.get("title") or "our next book"
    authors = ", ".join(book.get("authors") or []) or "the author"
    when = meeting.get("date") or "the meeting date"
    pickers = ", ".join(meeting.get("pickerNames") or [])
    picker_line = f" {pickers} picked it." if pickers else ""
    return (
        "Write the club's pre-meeting email that goes out two days before we meet, to the whole "
        "mailing list. Research it properly with your tools first, then write a rich, surprising "
        "email a long-time member would be glad to get.\n\n"
        f"Open with a short greeting and a one-line reminder that we meet on {when} to discuss "
        f"{title} by {authors}.{picker_line}\n\n"
        "Then three sections, each under its own '## ' header:\n\n"
        "## Connections\n"
        "3-5 provocations that set this book against OUR OWN reading history — real, specific, "
        "non-obvious connections to past club books, recurring arguments we've had, author or "
        "topic patterns, questions we've never settled. Ground these in what the club has actually "
        "read and discussed; use related_books, compare_books, review_summary, and "
        "search_mail_archive.\n\n"
        "## On the Book\n"
        "5-7 discussion questions about THIS book on its own terms — its arguments, methods, the "
        "author's choices, what's provocative, weak, or unresolved in it. These must NOT reference "
        "other club books; they're for digging into this one.\n\n"
        "## A third section — your call\n"
        "Give it your own header and surprise us: something genuinely interesting and unexpected "
        "about the book, its author, its making, its reception, a strange fact, or a debate it "
        "sparked. Use web_search if it helps you find a real, delightful angle this "
        "technically-minded club hasn't heard. Make it the part people forward to a friend.\n\n"
        "Format: plain-text email; a '## ' header for each of the three sections; *asterisks* "
        "around book titles; no other markdown. Write the ENTIRE email between <email> and "
        "</email> tags and put NOTHING outside them — no preamble, no notes, no sign-off (a "
        "signature is added automatically)."
    )


def topic_email(meeting: dict | None = None) -> dict:
    """The 2-day discussion-topics email. Subject + body (body includes the signature)."""
    meeting = meeting or meeting_rules.next_meeting()
    title = (meeting.get("book") or {}).get("title") or "our next book"
    when = meeting.get("date") or ""
    body = _extract_email(oliver.generate(topic_email_prompt(meeting)))  # signature added by outbound.send
    subject = f"Discussion topics for {title}" + (f" — meeting {when}" if when else "")
    return {"subject": subject, "body": body}


def week_reminder(meeting: dict | None = None, status: dict | None = None) -> dict:
    """The 1-week reminder: meeting + who's committed. Subject + body (with signature)."""
    meeting = meeting or meeting_rules.next_meeting()
    status = status or meeting_rules.meeting_status(meeting["meetingKey"])
    title = (meeting.get("book") or {}).get("title") or "our next book"
    when = meeting.get("date") or ""
    committed = _committed_names(status)
    fallback = (
        f"Hi all — a reminder that the R/W Book Club meets on {when} to discuss {title}.\n\n"
        + (f"Committed so far: {', '.join(committed)}.\n\n" if committed else "")
        + "If you haven't yet, let me know whether you can make it."
    )
    body = oliver.compose(
        "one-week-out reminder email to the whole club mailing list",
        {
            "occasion": "the monthly meeting is about a week away",
            "book": title,
            "meeting date": when,
            "committed to attend so far": ", ".join(committed) or "no one has confirmed yet",
            "ask": "anyone who hasn't responded should reply whether they can make it",
        },
        fallback=fallback,
        medium="email",
    )  # signature added by outbound.send
    subject = f"Reminder: {title} on {when}" if when else f"Reminder: {title}"
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
