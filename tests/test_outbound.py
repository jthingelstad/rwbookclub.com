"""The single outbound-email path: signature + send (pure; no contact log, no tracking)."""
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
