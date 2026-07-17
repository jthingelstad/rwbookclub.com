"""HTML email rendering for Oliver — markdown body → styled HTML for the email part.

No open tracking and no per-member contact log here: Oliver records its outbound asks as
`events` at the call site (where the meeting/member ids are known). This module is pure render.
"""

from __future__ import annotations

import html

import markdown as _markdown
from markdown.extensions import Extension as _Extension
from markdown.postprocessors import RawHtmlPostprocessor as _RawHtmlPostprocessor


class _EscapeRawHtmlPostprocessor(_RawHtmlPostprocessor):
    """Escape any raw HTML the model emitted instead of passing it through — so `<finished>`
    becomes the literal text `&lt;finished&gt;` and a stray `<script>` can never be live HTML."""

    def run(self, text: str) -> str:
        for i in range(self.md.htmlStash.html_counter):
            raw = self.md.htmlStash.rawHtmlBlocks[i]
            text = text.replace(self.md.htmlStash.get_placeholder(i), html.escape(str(raw)))
        return text


class _EscapeRawHtmlExtension(_Extension):
    def extendMarkdown(self, md) -> None:
        md.postprocessors.register(_EscapeRawHtmlPostprocessor(md), "raw_html", 30)


def _render_markdown(text: str) -> str:
    """Render Oliver's markdown body to email-safe HTML.

    Markdown does its own escaping: angle brackets in a `code span` come out as a proper
    `<code>&lt;…&gt;</code>` (NOT double-escaped to a visible `&amp;lt;`), bare angle brackets
    in prose stay literal, and the _EscapeRawHtml extension neutralizes any raw HTML the model
    emits (no live tags). Pre-escaping the text here would double-escape code spans, so don't —
    let the renderer handle it. We deliberately do NOT use `nl2br`: a stray single newline mid-
    sentence (the model sometimes echoes line breaks from search results) would otherwise become a
    hard <br>; standard markdown collapses it to a space instead. Paragraphs come from blank lines,
    and the signature carries its own HTML (see text_to_html), so no line-break extension is needed.
    """
    return _markdown.markdown(text or "", extensions=["sane_lists", _EscapeRawHtmlExtension()])


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
    # Signature footer — a quiet rule + muted rotating fact.
    ".oliver-sig{margin-top:28px;padding-top:12px;border-top:1px solid #e4e4e4;"
    "font-size:14px;color:#555;}"
    ".oliver-sig p{margin:0 0 4px;}"
    ".oliver-sig a{color:#2563eb;text-decoration:none;}"
    ".oliver-sig-fact{color:#8a8a8a;font-style:italic;}"
)


def text_to_html(text: str, *, signature_html: str | None = None) -> str:
    """Render the markdown body to an email-safe HTML document. `signature_html`, when given, is
    appended inside the wrapper as its own styled footer — it is trusted HTML built by
    signature.email_signature_html (NOT run through the markdown renderer), so the signature owns
    its formatting/links instead of depending on how the body is rendered."""
    body = _render_markdown(text) or "<p></p>"
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<style>{_EMAIL_CSS}</style></head>"
        '<body><div class="oliver-email">'
        f"{body}{signature_html or ''}</div></body></html>"
    )
