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

from agent import oliver
from agent.club import meeting_rules
from agent.mail import outbound


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
        "Write the club's pre-meeting email that goes out two days before we meet — to the "
        "whole mailing list. It has two jobs: (1) a brief final reminder that we meet on "
        f"{when} to discuss {title} by {authors}.{picker_line} (2) Three to five discussion "
        "provocations for this book that fold in OUR OWN reading history — real connections "
        "to past club books, recurring arguments we've had, author or topic patterns, "
        "questions we've never settled. Use your tools (related_books, compare_books, "
        "review_summary, search_mail_archive, get_book) to find specific, non-obvious "
        "connections grounded in what this club has actually read and discussed — not "
        "generic book-club questions. Provocations and connections, not a formal agenda.\n\n"
        "Format: plain-text email. You may use *asterisks* around book titles as usual, but "
        "NO markdown bold, NO headings, and NO '---' divider lines. Output ONLY the email "
        "itself, starting directly with a short greeting to the club — no subject line, no "
        "preamble, no notes to me, nothing before the greeting. Do not sign off; a signature "
        "is added automatically."
    )


def topic_email(meeting: dict | None = None) -> dict:
    """The 2-day discussion-topics email. Subject + body (body includes the signature)."""
    meeting = meeting or meeting_rules.next_meeting()
    title = (meeting.get("book") or {}).get("title") or "our next book"
    when = meeting.get("date") or ""
    body = oliver.generate(topic_email_prompt(meeting))  # signature added by outbound.send
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
