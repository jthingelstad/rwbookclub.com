"""The single outbound-email path for Oliver: signature, HTML, and send in one place.

Every email Oliver sends routes through send(): it appends his contextual signature
(unless sign=False), lets email_jmap render the markdown body into a formatted HTML part
(email_render.text_to_html), and submits via JMAP. Sending is pure — Oliver records its
outbound asks as `events` at the call site (where the meeting/member ids are known), so
this module no longer logs per-member contacts.
"""

from __future__ import annotations

from agent.mail import email_jmap, signature


def finalize(body: str, *, sign: bool = True) -> str:
    """The exact text that will be sent — body plus the signature. Use for previews."""
    if sign:
        return body.rstrip() + "\n\n" + signature.email_signature()
    return body


def send(*, to, subject: str, body: str, sign: bool = True,
         cc=None, in_reply_to: str | None = None, references=None) -> dict:
    """Append signature → render HTML → send. Returns the JMAP send result."""
    body = finalize(body, sign=sign)
    return email_jmap.send_email(
        to=to, subject=subject, body=body, cc=cc,
        in_reply_to=in_reply_to, references=references)
