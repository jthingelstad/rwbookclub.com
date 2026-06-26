"""The single outbound-email path: signature + tracking + send."""
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


def test_send_tracked_uses_prepare_and_marks_sent(monkeypatch):
    monkeypatch.setattr(outbound.signature, "email_signature", lambda: "SIG")
    monkeypatch.setattr(outbound.email_tracking, "prepare_outbound", lambda **kw: (7, "<html>", "tok"))
    monkeypatch.setattr(outbound.email_jmap, "send_email", lambda **kw: {"emailId": "e2"})
    marks = []
    monkeypatch.setattr(outbound.email_tracking, "mark_outbound_sent", lambda *a: marks.append(a))
    out = outbound.send(to=["a@b"], subject="S", body="Hi",
                        track={"meeting_key": "m", "member_slug": "jamie", "kind": "roll_call"})
    assert out["emailId"] == "e2"
    assert marks == [(7, "tok", "e2")]


def test_send_tracked_marks_failed_on_error(monkeypatch):
    monkeypatch.setattr(outbound.signature, "email_signature", lambda: "SIG")
    monkeypatch.setattr(outbound.email_tracking, "prepare_outbound", lambda **kw: (7, "<html>", "tok"))

    def boom(**kw):
        raise RuntimeError("send failed")

    monkeypatch.setattr(outbound.email_jmap, "send_email", boom)
    failed = []
    monkeypatch.setattr(outbound.email_tracking, "mark_outbound_failed", lambda *a: failed.append(a))
    with pytest.raises(RuntimeError):
        outbound.send(to=["a@b"], subject="S", body="Hi",
                      track={"meeting_key": "m", "member_slug": "jamie", "kind": "roll_call"})
    assert failed == [(7,)]
