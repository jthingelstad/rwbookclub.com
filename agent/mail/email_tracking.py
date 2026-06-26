"""HTML email rendering and optional open tracking for Oliver."""

from __future__ import annotations

import html
import secrets

import markdown as _markdown

from agent import config, db
from agent.mail import tinylytics


def _render_markdown(text: str) -> str:
    """Render Oliver's markdown body to email-safe HTML.

    The text is html-escaped first so any literal angle brackets stay literal (and no raw
    HTML from the model is ever emitted); markdown syntax (*italic*, **bold**, lists) is
    untouched by escaping and renders normally. `nl2br` keeps single newlines as breaks,
    matching how Oliver writes email paragraphs.
    """
    escaped = html.escape(text or "")
    return _markdown.markdown(escaped, extensions=["nl2br", "sane_lists"])


def enabled() -> bool:
    return tinylytics.enabled()


def tracking_url(token: str) -> str:
    return tinylytics.pixel_url(token)


def new_token() -> str:
    return secrets.token_urlsafe(18)


def text_to_html(text: str, *, tracking_url_value: str | None = None) -> str:
    body = _render_markdown(text) or "<p></p>"
    footer = ""
    pixel = ""
    if tracking_url_value:
        footer = (
            "<p style=\"font-size:12px;color:#666;margin-top:24px;\">"
            "Oliver records whether this email was opened so the club can avoid unnecessary follow-ups."
            "</p>"
        )
        pixel = (
            f"<img src=\"{html.escape(tracking_url_value)}\" width=\"1\" height=\"1\" "
            "alt=\"\" style=\"display:none;width:1px;height:1px;border:0;\" />"
        )
    return (
        "<!doctype html><html><body "
        "style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "font-size:16px;line-height:1.45;color:#222;\">"
        f"{body}{footer}{pixel}</body></html>"
    )


def tracked_html(*, text: str, contact_id: int, meeting_key: str,
                 member_slug: str, kind: str, subject: str) -> tuple[str | None, str | None]:
    if not enabled():
        return (text_to_html(text) if config.OLIVER_EMAIL_HTML_ENABLED else None, None)
    token = new_token()
    db.add_email_tracking(
        token=token,
        contact_id=contact_id,
        meeting_key=meeting_key,
        member_slug=member_slug,
        kind=kind,
        subject=subject,
    )
    return text_to_html(text, tracking_url_value=tracking_url(token)), token


def prepare_outbound(*, text: str, meeting_key: str, member_slug: str,
                     kind: str, subject: str) -> tuple[int, str | None, str | None]:
    contact_id = db.add_member_contact(
        meeting_key=meeting_key,
        member_slug=member_slug,
        kind=kind,
        surface="email",
        direction="outbound",
        status="sending",
        subject=subject,
    )
    html_body, token = tracked_html(
        text=text,
        contact_id=contact_id,
        meeting_key=meeting_key,
        member_slug=member_slug,
        kind=kind,
        subject=subject,
    )
    return contact_id, html_body, token


def mark_outbound_sent(contact_id: int, token: str | None, email_id: str | None) -> None:
    db.update_member_contact_status(contact_id, "sent")
    if token:
        db.mark_email_tracking_sent(token, email_id)


def mark_outbound_failed(contact_id: int) -> None:
    db.update_member_contact_status(contact_id, "failed")
