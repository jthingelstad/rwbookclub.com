"""The single outbound-email path for Oliver: signature, HTML, and send in one place.

Every email Oliver sends routes through send(): it appends his contextual signature
(unless sign=False), lets email_jmap render the markdown body into a formatted HTML part
(email_tracking.text_to_html), and submits via JMAP. For per-member tracked mail
(open-pixel + contact record) pass `track={meeting_key, member_slug, kind}`; the contact
row is marked sent/failed here too. Omit `track` for list / personal / reply mail.
"""

from __future__ import annotations

from agent.mail import email_jmap, email_tracking, signature


def finalize(body: str, *, sign: bool = True) -> str:
    """The exact text that will be sent — body plus the signature. Use for previews."""
    if sign:
        return body.rstrip() + "\n\n" + signature.email_signature()
    return body


def send(*, to, subject: str, body: str, sign: bool = True, track: dict | None = None,
         cc=None, in_reply_to: str | None = None, references=None) -> dict:
    """Append signature → render HTML → send. Returns the JMAP send result."""
    body = finalize(body, sign=sign)
    if track is not None:
        contact_id, html_body, token = email_tracking.prepare_outbound(
            text=body, subject=subject, **track)
        try:
            sent = email_jmap.send_email(
                to=to, subject=subject, body=body, html_body=html_body,
                cc=cc, in_reply_to=in_reply_to, references=references)
        except Exception:
            email_tracking.mark_outbound_failed(contact_id)
            raise
        email_tracking.mark_outbound_sent(contact_id, token, sent.get("emailId"))
        return sent
    return email_jmap.send_email(
        to=to, subject=subject, body=body, cc=cc,
        in_reply_to=in_reply_to, references=references)
