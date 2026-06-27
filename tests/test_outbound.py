"""The single outbound-email path: signature + optional contact log + send (no tracking)."""
import pytest

from agent.mail import outbound


def test_finalize_appends_signature(monkeypatch):
    monkeypatch.setattr(outbound.signature, "email_signature", lambda: "— Oliver\nSIG")
    assert outbound.finalize("Hello") == "Hello\n\n— Oliver\nSIG"
    assert outbound.finalize("Hello", sign=False) == "Hello"


def test_send_untracked_signs_and_sends(monkeypatch):
    captured = {}
    monkeypatch.setattr(outbound.signature, "email_signature", lambda: "SIG")
    monkeypatch.setattr(outbound.email_jmap, "send_email",
                        lambda **kw: captured.update(kw) or {"emailId": "e1"})
    out = outbound.send(to=["a@b"], subject="S", body="Hi")
    assert captured["body"] == "Hi\n\nSIG"        # signature appended once, centrally
    assert captured["to"] == ["a@b"]
    assert out["emailId"] == "e1"


def test_send_with_contact_logs_and_marks_sent(monkeypatch):
    monkeypatch.setattr(outbound.signature, "email_signature", lambda: "SIG")
    monkeypatch.setattr(outbound.email_render, "prepare_outbound", lambda **kw: (7, "<html>"))
    monkeypatch.setattr(outbound.email_jmap, "send_email", lambda **kw: {"emailId": "e2"})
    marks = []
    monkeypatch.setattr(outbound.email_render, "mark_outbound_sent", lambda *a: marks.append(a))
    out = outbound.send(to=["a@b"], subject="S", body="Hi",
                        contact={"meeting_id": 1, "member_id": 2, "kind": "roll_call"})
    assert out["emailId"] == "e2"
    assert marks == [(7,)]


def test_send_with_contact_marks_failed_on_error(monkeypatch):
    monkeypatch.setattr(outbound.signature, "email_signature", lambda: "SIG")
    monkeypatch.setattr(outbound.email_render, "prepare_outbound", lambda **kw: (7, "<html>"))

    def boom(**kw):
        raise RuntimeError("send failed")

    monkeypatch.setattr(outbound.email_jmap, "send_email", boom)
    failed = []
    monkeypatch.setattr(outbound.email_render, "mark_outbound_failed", lambda *a: failed.append(a))
    with pytest.raises(RuntimeError):
        outbound.send(to=["a@b"], subject="S", body="Hi",
                      contact={"meeting_id": 1, "member_id": 2, "kind": "roll_call"})
    assert failed == [(7,)]
