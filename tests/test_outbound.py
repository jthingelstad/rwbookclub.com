"""The single outbound-email path: signature, durable intent, policy, and send."""
import pytest

from agent import db
from agent.mail import outbound


def test_finalize_appends_signature(monkeypatch):
    monkeypatch.setattr(outbound.signature, "email_signature", lambda: "— Oliver\nSIG")
    assert outbound.finalize("Hello") == "Hello\n\n— Oliver\nSIG"
    assert outbound.finalize("Hello", sign=False) == "Hello"


def test_send_multipart_text_and_html(monkeypatch):
    captured = {}
    monkeypatch.setattr(outbound.signature, "email_signatures",
                        lambda: ("SIG", '<div class="oliver-sig">SIG</div>'))
    monkeypatch.setattr(outbound.email_jmap, "send_email",
                        lambda **kw: captured.update(kw) or {"emailId": "e1"})
    out = outbound.send(to=["a@b"], subject="S", body="Hi")
    assert captured["body"] == "Hi\n\nSIG"                 # plain-text part: body + plain signature
    assert '<div class="oliver-sig">SIG</div>' in captured["html_body"]  # HTML sig footer injected
    assert "<p>Hi</p>" in captured["html_body"]            # body rendered from markdown
    assert captured["to"] == ["a@b"]
    assert out["emailId"] == "e1"


def test_send_unsigned_has_no_signature(monkeypatch):
    captured = {}
    monkeypatch.setattr(outbound.email_jmap, "send_email",
                        lambda **kw: captured.update(kw) or {"emailId": "e1"})
    outbound.send(to=["a@b"], subject="S", body="Hi", sign=False)
    assert captured["body"] == "Hi"
    assert '<div class="oliver-sig">' not in captured["html_body"]  # no signature block (CSS aside)


def test_linked_member_policy_is_checked_before_delivery(fresh_db, monkeypatch):
    calls = []
    monkeypatch.setattr(outbound.email_jmap, "send_email", lambda **kw: calls.append(kw) or {})
    with pytest.raises(ValueError, match="linked member"):
        outbound.send(
            to=["outsider@example.test"], subject="S", body="Hi",
            policy="linked_member", idempotency_key="email:policy-denied",
        )
    assert calls == []
    assert db.outbox_by_key("email:policy-denied") is None

    fresh_db.link_member_email("jamie@example.test", "jamie")
    outbound.send(
        to=["jamie@example.test"], subject="S", body="Hi",
        policy="linked_member", idempotency_key="email:policy-allowed",
    )
    assert len(calls) == 1
