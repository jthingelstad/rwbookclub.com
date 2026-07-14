"""The single outbound-email path for Oliver: signature, HTML, and send in one place.

Every email Oliver sends routes through send(): it appends his contextual signature
(unless sign=False), lets email_jmap render the markdown body into a formatted HTML part
(email_render.text_to_html), and submits via JMAP. Sending is pure — Oliver records its
outbound asks as `events` at the call site (where the meeting/member ids are known), so
this module no longer logs per-member contacts.
"""

from __future__ import annotations

import json

from agent import config, outbox
from agent.mail import email_jmap, email_policy, email_render, signature


def finalize(body: str, *, sign: bool = True) -> str:
    """The exact plain-text that will be sent — body plus the plain-text signature. Use for
    previews (the HTML part is rendered separately in send())."""
    if sign:
        return body.rstrip() + "\n\n" + signature.email_signature()
    return body


def _validate(payload: dict) -> None:
    to = payload["to"]
    cc = payload.get("cc")
    recipients = email_policy.parse_addresses(to) + email_policy.parse_addresses(cc)
    if not recipients:
        raise ValueError("at least one recipient email address is required")
    policy = payload.get("policy") or "trusted"
    if policy == "model":
        if error := email_policy.validate_model_email_recipients(to=to, cc=cc):
            raise ValueError(error)
    elif policy == "linked_member":
        if any(email_policy.is_mailing_list_address(address) for address in recipients):
            raise ValueError("linked-member email policy cannot target the mailing list")
        if any(not email_policy.is_known_member_address(address) for address in recipients):
            raise ValueError("linked-member email policy requires linked member recipients")
    elif policy == "cadence":
        mailing_list = email_policy.configured_mailing_list_address()
        if recipients != [mailing_list]:
            raise ValueError("cadence email policy requires only the configured mailing list")
    elif policy == "reply":
        if any(not (email_policy.is_known_member_address(address)
                   or email_policy.is_mailing_list_address(address)) for address in recipients):
            raise ValueError("reply email policy requires a linked member or the mailing list")
    elif policy != "trusted":
        raise ValueError(f"unknown outbound email policy: {policy}")


def send(*, to, subject: str, body: str, sign: bool = True,
         cc=None, in_reply_to: str | None = None, references=None,
         idempotency_key: str | None = None, policy: str = "trusted") -> dict:
    """Send as multipart: a plain-text part (body + plain signature) and an HTML part (the markdown
    body rendered, with the signature's own HTML footer injected). Building the two parts here —
    rather than signing the body and letting the renderer turn it into HTML — is what lets the
    signature carry proper HTML/links instead of leaning on the renderer's line breaks. Returns the
    JMAP send result."""
    text_sig, html_sig = signature.email_signatures() if sign else (None, None)
    text_body = f"{body.rstrip()}\n\n{text_sig}" if text_sig else body
    html_body = (email_render.text_to_html(body, signature_html=html_sig)
                 if config.OLIVER_EMAIL_HTML_ENABLED else None)
    payload = {
        "to": to,
        "subject": subject,
        "body": text_body,
        "html_body": html_body,
        "cc": cc,
        "in_reply_to": in_reply_to,
        "references": references,
        "policy": policy,
    }
    _validate(payload)  # enqueue boundary
    row = outbox.enqueue(
        kind="email",
        payload=payload,
        idempotency_key=idempotency_key,
    )

    return deliver_outbox_row(row)


def deliver_outbox_row(row: dict) -> dict:
    """Narrow email-provider worker boundary, also used to drain persisted retries."""
    payload = json.loads(row["payload_json"])

    def _deliver() -> dict:
        _validate(payload)  # delivery boundary; links/config may have changed since enqueue
        provider_payload = {k: v for k, v in payload.items() if k != "policy"}
        return email_jmap.send_email(**provider_payload)

    return outbox.deliver_sync(
        row,
        _deliver,
        retryable_errors=(email_jmap.JMAPError,),
    )
