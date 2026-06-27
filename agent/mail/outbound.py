"""The single outbound-email path for Oliver: signature, HTML, and send in one place.

Every email Oliver sends routes through send(): it appends his contextual signature
(unless sign=False), lets email_jmap render the markdown body into a formatted HTML part
(email_render.text_to_html), and submits via JMAP. For per-member mail pass
`contact={meeting_id, member_id, kind}` to log the outbound contact in member_contacts
(marked sent/failed here); there is no open tracking. Omit `contact` for list / personal /
reply mail.
"""

from __future__ import annotations

from agent.mail import email_jmap, email_render, signature


def finalize(body: str, *, sign: bool = True) -> str:
    """The exact text that will be sent — body plus the signature. Use for previews."""
    if sign:
        return body.rstrip() + "\n\n" + signature.email_signature()
    return body


def send(*, to, subject: str, body: str, sign: bool = True, contact: dict | None = None,
         cc=None, in_reply_to: str | None = None, references=None) -> dict:
    """Append signature → render HTML → send. Returns the JMAP send result.

    Pass `contact={meeting_id, member_id, kind}` to log the send in member_contacts
    (sent/failed). No open tracking is performed.
    """
    body = finalize(body, sign=sign)
    if contact is not None:
        contact_id, html_body = email_render.prepare_outbound(
            text=body, subject=subject, **contact)
        try:
            sent = email_jmap.send_email(
                to=to, subject=subject, body=body, html_body=html_body,
                cc=cc, in_reply_to=in_reply_to, references=references)
        except Exception:
            email_render.mark_outbound_failed(contact_id)
            raise
        email_render.mark_outbound_sent(contact_id)
        return sent
    return email_jmap.send_email(
        to=to, subject=subject, body=body, cc=cc,
        in_reply_to=in_reply_to, references=references)
