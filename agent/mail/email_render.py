"""HTML email rendering for Oliver, plus the per-member outbound contact log.

No open tracking: Oliver does not record whether a member opens an email (no pixel, no
external poll). `prepare_outbound`/`mark_outbound_*` only write the operational `member_contacts`
row (we emailed X for roll-call/reading-checkin; status sent/failed) the campaign view reads.
"""

from __future__ import annotations

import html

import markdown as _markdown

from agent import config, db


def _render_markdown(text: str) -> str:
    """Render Oliver's markdown body to email-safe HTML.

    The text is html-escaped first so any literal angle brackets stay literal (and no raw
    HTML from the model is ever emitted); markdown syntax (*italic*, **bold**, lists) is
    untouched by escaping and renders normally. `nl2br` keeps single newlines as breaks,
    matching how Oliver writes email paragraphs.
    """
    escaped = html.escape(text or "")
    return _markdown.markdown(escaped, extensions=["nl2br", "sane_lists"])


# Email CSS. Delivered as a <style> block (well-supported in Apple Mail, Gmail, and
# most modern clients); a .oliver-email wrapper scopes it so it can't bleed into quoted
# replies. Tuned for a readable long-form discussion email: clear section headers,
# breathing room between numbered points.
_EMAIL_CSS = (
    ".oliver-email{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,"
    "Arial,sans-serif;font-size:16px;line-height:1.55;color:#2a2a2a;max-width:640px;"
    "margin:0 auto;padding:8px 2px;}"
    ".oliver-email p{margin:0 0 16px;}"
    ".oliver-email h2{font-size:20px;font-weight:600;color:#111;margin:34px 0 14px;"
    "padding-bottom:6px;border-bottom:2px solid #e4e4e4;}"
    ".oliver-email h3{font-size:17px;font-weight:600;color:#111;margin:28px 0 10px;}"
    ".oliver-email ol,.oliver-email ul{padding-left:24px;margin:0 0 16px;}"
    ".oliver-email li{margin-bottom:12px;padding-left:4px;}"
    ".oliver-email strong{font-weight:600;color:#111;}"
    ".oliver-email hr{border:0;border-top:1px solid #ececec;margin:24px 0;}"
    ".oliver-email a{color:#2563eb;}"
)


def text_to_html(text: str) -> str:
    body = _render_markdown(text) or "<p></p>"
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<style>{_EMAIL_CSS}</style></head>"
        "<body><div class=\"oliver-email\">"
        f"{body}</div></body></html>"
    )


def prepare_outbound(*, text: str, meeting_id: int, member_id: int,
                     kind: str, subject: str) -> tuple[int, str | None]:
    """Log the outbound contact in member_contacts and render the HTML body. No tracking."""
    contact_id = db.add_member_contact(
        meeting_id=meeting_id,
        member_id=member_id,
        kind=kind,
        surface="email",
        direction="outbound",
        status="sending",
        subject=subject,
    )
    html_body = text_to_html(text) if config.OLIVER_EMAIL_HTML_ENABLED else None
    return contact_id, html_body


def mark_outbound_sent(contact_id: int) -> None:
    db.update_member_contact_status(contact_id, "sent")


def mark_outbound_failed(contact_id: int) -> None:
    db.update_member_contact_status(contact_id, "failed")
