"""The single outbound-email path for Oliver: signature, HTML, and send in one place.

Every email Oliver sends routes through send(): it appends his contextual signature
(unless sign=False), lets email_jmap render the markdown body into a formatted HTML part
(email_render.text_to_html), and submits via JMAP. Sending is pure — Oliver records its
outbound asks as `events` at the call site (where the meeting/member ids are known), so
this module no longer logs per-member contacts.
"""

from __future__ import annotations

from agent import config
from agent.mail import email_jmap, email_render, signature


def finalize(body: str, *, sign: bool = True) -> str:
    """The exact plain-text that will be sent — body plus the plain-text signature. Use for
    previews (the HTML part is rendered separately in send())."""
    if sign:
        return body.rstrip() + "\n\n" + signature.email_signature()
    return body


def send(*, to, subject: str, body: str, sign: bool = True,
         cc=None, in_reply_to: str | None = None, references=None) -> dict:
    """Send as multipart: a plain-text part (body + plain signature) and an HTML part (the markdown
    body rendered, with the signature's own HTML footer injected). Building the two parts here —
    rather than signing the body and letting the renderer turn it into HTML — is what lets the
    signature carry proper HTML/links instead of leaning on the renderer's line breaks. Returns the
    JMAP send result."""
    text_sig, html_sig = signature.email_signatures() if sign else (None, None)
    text_body = f"{body.rstrip()}\n\n{text_sig}" if text_sig else body
    html_body = (email_render.text_to_html(body, signature_html=html_sig)
                 if config.OLIVER_EMAIL_HTML_ENABLED else None)
    return email_jmap.send_email(
        to=to, subject=subject, body=text_body, html_body=html_body, cc=cc,
        in_reply_to=in_reply_to, references=references)
