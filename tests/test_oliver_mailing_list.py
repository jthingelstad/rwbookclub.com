"""Mailing-list reply gate in Oliver's agent loop."""

from __future__ import annotations

from agent import oliver
from agent.mail.email_jmap import InboundEmail


def msg(text: str) -> InboundEmail:
    return InboundEmail(
        id="m1",
        thread_id="t1",
        message_id="msg1@example.test",
        from_name="Tom",
        from_email="tom@tomeri.org",
        to=["rwbookclub@googlegroups.com"],
        cc=[],
        reply_to=["rwbookclub@googlegroups.com"],
        subject="Re: [rwbookclub] Meeting in 5 days!",
        text=text,
        received_at="2026-06-25T13:00:00Z",
        references=[],
    )


def test_mailing_list_no_reply_sentinel(monkeypatch):
    calls = []

    def fake_answer(question, **kwargs):
        calls.append((question, kwargs))
        return "[[NO_REPLY: bare_mention]]"

    monkeypatch.setattr(oliver, "answer", fake_answer)
    result = oliver.answer_mailing_list_email(
        msg("Not sure I've seen anything from Oliver recently."),
        channel_id="email:list:t1",
        speaker="Tom",
        speaker_user_id="email:tom@tomeri.org",
        source_message_id="m1",
    )

    assert result.reply is False
    assert result.body == ""
    assert result.reason == "bare_mention"
    assert calls[0][1]["channel_id"] == "email:list:t1"
    assert calls[0][1]["source_message_id"] == "m1"
    # A mailing-list reply is an email — forward the email voice + headroom.
    assert calls[0][1]["medium"] == "email"
    assert calls[0][1]["max_tokens"] == oliver.EMAIL_MAX_TOKENS
    assert calls[0][1]["persist"] is False  # the internal decision turn must not pollute channel memory
    assert "reply exactly `[[NO_REPLY: short_reason]]`" in calls[0][0]


def test_mailing_list_reply_body(monkeypatch):
    monkeypatch.setattr(oliver, "answer", lambda *args, **kwargs: "We read The Real North Korea in 2018.")

    result = oliver.answer_mailing_list_email(
        msg("Oliver, what North Korea book did we read?"),
        channel_id="email:list:t1",
        speaker="Tom",
        speaker_user_id="email:tom@tomeri.org",
        source_message_id="m1",
    )

    assert result.reply is True
    assert result.body == "We read The Real North Korea in 2018."
    assert result.reason is None


def test_mailing_list_prompt_uses_unquoted_visible_text(monkeypatch):
    calls = []

    def fake_answer(question, **kwargs):
        calls.append(question)
        return "[[NO_REPLY: status_update]]"

    monkeypatch.setattr(oliver, "answer", fake_answer)
    oliver.answer_mailing_list_email(
        msg(
            '<html><body><p>I will miss it.</p><blockquote type="cite">'
            "On Jun 25, Oliver wrote:<br>Anything I should answer?"
            "</blockquote></body></html>"
        ),
        channel_id="email:list:t1",
    )

    assert "I will miss it." in calls[0]
    assert "Anything I should answer?" not in calls[0]


def test_explicit_member_identity_token_resolves_member():
    assert oliver._resolve_member("R/W Book Club", "member:jamie") == "jamie"
    assert oliver._resolve_member("R/W Book Club", "member:not-a-member") is None
