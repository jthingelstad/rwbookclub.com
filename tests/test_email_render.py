"""HTML email rendering + the per-member outbound contact log (no open tracking)."""

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


def test_prepare_outbound_logs_contact_without_tracking(fresh_db):
    from agent import clubdb
    mid = clubdb.meeting_id_for_book_slug("a-world-appears")
    jamie = clubdb.lookup_member_id("jamie")
    contact_id, html = email_render.prepare_outbound(
        text="Hello", meeting_id=mid, member_id=jamie, kind="roll_call", subject="Roll call",
    )
    assert contact_id > 0
    assert "<img" not in (html or "")          # rendered HTML carries no pixel
    contacts = fresh_db.member_contacts_for_meeting(mid)
    assert contacts[0]["status"] == "sending"
    email_render.mark_outbound_sent(contact_id)
    assert fresh_db.member_contacts_for_meeting(mid)[0]["status"] == "sent"
    email_render.mark_outbound_failed(contact_id)
    assert fresh_db.member_contacts_for_meeting(mid)[0]["status"] == "failed"
