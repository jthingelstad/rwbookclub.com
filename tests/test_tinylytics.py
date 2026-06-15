"""Tinylytics email-open sync client."""

from __future__ import annotations


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_pixel_url_uses_path_token(monkeypatch):
    from agent import config, tinylytics

    monkeypatch.setattr(config, "TINYLYTICS_SITE_ID", "site code")
    url = tinylytics.pixel_url("tok1")
    assert url.startswith("https://tinylytics.app/pixel/site%20code.gif?")
    assert "path=%2Foliver%2Femail%2Ftok1" in url


def test_sync_email_opens_marks_seen(monkeypatch, fresh_db):
    from agent import config, db, tinylytics

    monkeypatch.setattr(config, "TINYLYTICS_SITE_ID", "site-code")
    monkeypatch.setattr(config, "TINYLYTICS_SITE_ID_NUMERIC", "123")
    monkeypatch.setattr(config, "TINYLYTICS_API_KEY", "tly-ro-test")

    cid = db.add_member_contact(
        meeting_key="a-world-appears",
        member_slug="jamie",
        kind="reading_checkin",
        surface="email",
        direction="outbound",
        status="sent",
        subject="Reading check-in",
    )
    db.add_email_tracking(
        token="tok1",
        contact_id=cid,
        meeting_key="a-world-appears",
        member_slug="jamie",
        kind="reading_checkin",
        subject="Reading check-in",
    )

    calls = []

    def fake_get(url, *, params, headers, timeout):
        calls.append((url, params, headers, timeout))
        return FakeResponse({"hits": [], "pagination": {"total_count": 1}})

    monkeypatch.setattr(tinylytics.requests, "get", fake_get)
    assert tinylytics.sync_email_opens() == 1
    assert calls[0][1]["path"] == "/oliver/email/tok1"
    assert calls[0][2]["Authorization"] == "Bearer tly-ro-test"
    assert db.email_open_summary("a-world-appears")["jamie"]["open_count"] == 1
