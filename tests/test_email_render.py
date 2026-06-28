"""HTML email rendering (pure render — no open tracking, no contact log)."""

from __future__ import annotations

from agent.mail import email_render


def test_text_to_html_renders_markdown():
    html = email_render.text_to_html("Read *Stiff* by **Mary Roach**\n\n- one\n- two")
    assert "<em>Stiff</em>" in html
    assert "<strong>Mary Roach</strong>" in html
    assert "<li>one</li>" in html and "<li>two</li>" in html


def test_text_to_html_escapes_and_has_no_tracking_pixel():
    html = email_render.text_to_html("Hi Jamie,\n\n<finished>")
    assert "&lt;finished&gt;" in html          # body html-escaped
    assert "<img" not in html                  # no open-pixel
    assert "was opened" not in html            # no tracking-notice footer


def test_code_span_angle_brackets_not_double_escaped():
    # Regression: a `<subcommand>` code span must render as <code>&lt;subcommand&gt;</code>,
    # NOT the double-escaped <code>&amp;lt;subcommand&amp;gt;</code> that shows as a visible
    # "&lt;subcommand&gt;" in the mail client.
    html = email_render.text_to_html("Run the `<subcommand>` group.")
    assert "<code>&lt;subcommand&gt;</code>" in html
    assert "&amp;lt;" not in html


def test_raw_html_is_neutralized():
    # Defense-in-depth: the model can't emit live HTML even outside a code span.
    html = email_render.text_to_html("a <script>alert(1)</script> b")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_ampersand_in_prose_renders_once():
    assert "Tom &amp; Jerry" in email_render.text_to_html("Tom & Jerry")
    assert "&amp;amp;" not in email_render.text_to_html("Tom & Jerry")
