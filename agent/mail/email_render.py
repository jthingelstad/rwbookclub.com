"""HTML email rendering for Oliver — markdown body → styled HTML for the email part.

No open tracking and no per-member contact log here: Oliver records its outbound asks as
`events` at the call site (where the meeting/member ids are known). This module is pure render.
"""

from __future__ import annotations

import html

import markdown as _markdown


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
