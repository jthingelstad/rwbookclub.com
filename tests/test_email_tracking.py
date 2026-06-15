"""HTML email rendering and open tracking helpers."""

from __future__ import annotations


def test_text_to_html_escapes_and_includes_tracking_notice():
    from agent import email_tracking

    html = email_tracking.text_to_html(
        "Hi Jamie,\n\n<finished>",
        tracking_url_value="https://tinylytics.app/pixel/site.gif?path=%2Foliver%2Femail%2Ftok",
    )
    assert "&lt;finished&gt;" in html
    assert "https://tinylytics.app/pixel/site.gif?path=%2Foliver%2Femail%2Ftok" in html
    assert "records whether this email was opened" in html


def test_prepare_outbound_creates_contact_and_tracking(monkeypatch, fresh_db):
    from agent import config, email_tracking

    monkeypatch.setattr(config, "TINYLYTICS_SITE_ID", "site-code")
    monkeypatch.setattr(config, "TINYLYTICS_SITE_ID_NUMERIC", "123")
    monkeypatch.setattr(config, "TINYLYTICS_API_KEY", "tly-ro-test")
    contact_id, html, token = email_tracking.prepare_outbound(
        text="Hello",
        meeting_key="a-world-appears",
        member_slug="jamie",
        kind="roll_call",
        subject="Roll call",
    )
    assert contact_id > 0
    assert token
    assert f"path=%2Foliver%2Femail%2F{token}" in html
    contacts = fresh_db.member_contacts_for_meeting("a-world-appears")
    assert contacts[0]["status"] == "sending"
    email_tracking.mark_outbound_sent(contact_id, token, "email1")
    contacts = fresh_db.member_contacts_for_meeting("a-world-appears")
    assert contacts[0]["status"] == "sent"


def test_prepare_outbound_prefers_tinylytics_pixel(monkeypatch, fresh_db):
    from agent import config, email_tracking

    monkeypatch.setattr(config, "TINYLYTICS_SITE_ID", "site-code")
    monkeypatch.setattr(config, "TINYLYTICS_SITE_ID_NUMERIC", "123")
    monkeypatch.setattr(config, "TINYLYTICS_API_KEY", "tly-ro-test")
    contact_id, html, token = email_tracking.prepare_outbound(
        text="Hello",
        meeting_key="a-world-appears",
        member_slug="jamie",
        kind="roll_call",
        subject="Roll call",
    )
    assert contact_id > 0
    assert token
    assert "https://tinylytics.app/pixel/site-code.gif" in html
    assert f"path=%2Foliver%2Femail%2F{token}" in html
