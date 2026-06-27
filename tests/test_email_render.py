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
