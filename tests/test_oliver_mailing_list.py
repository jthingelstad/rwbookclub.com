"""Mailing-list reply gate in Oliver's agent loop."""

from __future__ import annotations

from agent import corpus_read as cr
from agent import oliver
from agent.mail.email_jmap import InboundEmail


def msg(
    text: str,
    *,
    from_name: str = "Tom",
    from_email: str = "tom@tomeri.org",
) -> InboundEmail:
    return InboundEmail(
        id="m1",
        thread_id="t1",
        message_id="msg1@example.test",
        from_name=from_name,
        from_email=from_email,
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
        msg("Oliver, can you decide whether this needs a reply?"),
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
    assert (
        calls[0][1]["persist"] is False
    )  # the internal decision turn must not pollute channel memory
    assert "reply exactly `[[NO_REPLY: short_reason]]`" in calls[0][0]


def test_unaddressed_mailing_list_posts_never_reach_generation(monkeypatch):
    calls = []

    def fake_answer(*args, **kwargs):
        calls.append((args, kwargs))
        return "This should never be generated."

    monkeypatch.setattr(oliver, "answer", fake_answer)
    unaddressed = (
        "P.S. Oliver now handles member pronouns. ;-)",
        "We'll meet Tuesday at 6:00 at Jamie's. The book is still TBD.",
        "We'll meet Tuesday at 6:00.\n\nOn Jul 20, Oliver wrote:\n> Oliver, what book is next?",
        "Does anyone know whether Oliver emailed Nick?",
        "Thanks for sorting this out.\n\n-- \nOliver",
    )

    for text in unaddressed:
        result = oliver.answer_mailing_list_email(
            msg(text),
            channel_id="email:list:t1",
            speaker="Tom",
            speaker_user_id="email:tom@tomeri.org",
            source_message_id="m1",
        )
        assert result == oliver.MailingListEmailResult(False, "", "not_explicitly_addressed")

    assert calls == []


def test_mailing_list_reply_body(monkeypatch):
    monkeypatch.setattr(
        oliver, "answer", lambda *args, **kwargs: "We read The Real North Korea in 2018."
    )

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
            "<html><body><p>Oliver, can you confirm I am marked absent?</p>"
            '<blockquote type="cite">'
            "On Jun 25, Oliver wrote:<br>Anything I should answer?"
            "</blockquote></body></html>"
        ),
        channel_id="email:list:t1",
    )

    assert "Oliver, can you confirm I am marked absent?" in calls[0]
    assert "Anything I should answer?" not in calls[0]


def test_mailing_list_prompt_preserves_trusted_linked_sender(monkeypatch):
    calls = []

    def fake_answer(question, **kwargs):
        calls.append((question, kwargs))
        return "You're already confirmed, Jamie."

    monkeypatch.setattr(oliver, "answer", fake_answer)
    result = oliver.answer_mailing_list_email(
        msg(
            "Oliver, can you confirm my attendance?",
            from_name="'Jamie Thingelstad' via rwbookclub",
            from_email="rwbookclub@googlegroups.com",
        ),
        channel_id="email:list:t1",
        speaker="'Jamie Thingelstad' via rwbookclub",
        speaker_user_id="member:jamie",
        source_message_id="m1",
    )

    expected_name = cr.find_member("jamie")["name"]
    assert result.reply is True
    assert calls[0][1]["speaker"] == expected_name
    assert f"Trusted linked sender: {expected_name} (member: jamie)" in calls[0][0]
    assert "never ask them to obtain their own confirmation" in calls[0][0]


def test_passing_mention_note_shape():
    # The Discord name-only restraint gate reuses the mailing-list sentinel contract.
    note = oliver.PASSING_MENTION_NOTE
    assert oliver.NO_REPLY_PREFIX in note
    assert "Err on silence" in note and "talking ABOUT you" in note
    # The bot's sentinel check must tolerate backtick-wrapped output (models do this).
    for raw in ("[[NO_REPLY: passing_reference]]", "`[[NO_REPLY: praise]]`"):
        assert raw.strip().strip("`").startswith(oliver.NO_REPLY_PREFIX)


def test_explicit_member_identity_token_resolves_member():
    assert oliver._resolve_member("R/W Book Club", "member:jamie") == "jamie"
    assert oliver._resolve_member("R/W Book Club", "member:not-a-member") is None
