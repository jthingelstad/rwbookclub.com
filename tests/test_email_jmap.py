"""Fastmail JMAP client helpers — pure unit tests, no network."""

from __future__ import annotations

from agent.email_jmap import InboundEmail, JMAPClient, JMAPError, _addresses, _text_body


class FakeJMAPClient(JMAPClient):
    def __init__(self):
        self.calls = []

    @property
    def session(self):
        return {
            "apiUrl": "https://example.test/jmap",
            "primaryAccounts": {
                "urn:ietf:params:jmap:mail": "mail-account",
                "urn:ietf:params:jmap:submission": "submission-account",
            },
            "accounts": {},
        }

    @property
    def identity_id(self):
        return "identity-oliver"

    def call(self, method_calls, *, using=None):
        self.calls.append((method_calls, using))
        first = method_calls[0][0]
        if first == "Mailbox/get":
            return [[
                "Mailbox/get",
                {"list": [
                    {"id": "inbox", "name": "Inbox", "parentId": None, "role": "inbox"},
                    {"id": "inbox-oliver", "name": "Oliver", "parentId": "inbox", "role": None},
                    {"id": "sent", "name": "Sent", "parentId": None, "role": "sent"},
                    {"id": "sent-oliver", "name": "Oliver", "parentId": "sent", "role": None},
                    {"id": "drafts", "name": "Drafts", "parentId": None, "role": "drafts"},
                ]},
                "mailboxes",
            ]]
        if first == "Email/query":
            return [
                ["Email/query", {"ids": ["m1"]}, "query"],
                ["Email/get", {"list": [{
                    "id": "m1",
                    "threadId": "t1",
                    "messageId": ["msg1@example.test"],
                    "from": [{"name": "Jamie", "email": "jamie@example.test"}],
                    "to": [{"name": "Oliver", "email": "oliver@rwbookclub.com"}],
                    "cc": [],
                    "replyTo": [],
                    "subject": "Question",
                    "receivedAt": "2026-06-09T12:00:00Z",
                    "textBody": [{"partId": "text"}],
                    "bodyValues": {"text": {"value": "Hello Oliver"}},
                    "references": ["prior@example.test"],
                }]}, "get"],
            ]
        if first == "Email/set" and len(method_calls) == 2:
            return [
                ["Email/set", {"created": {"oliverDraft": {"id": "draft1", "threadId": "thread1"}}}, "create"],
                ["EmailSubmission/set", {"created": {"oliverSend": {"id": "submission1"}}}, "submit"],
            ]
        if first == "Email/set":
            return [["Email/set", {"updated": {"m1": None}}, "mark"]]
        raise AssertionError(method_calls)


def test_addresses_parses_and_dedupes():
    assert _addresses(["Jamie <Jamie@Example.TEST>", "jamie@example.test", "bad"]) == [
        {"name": "Jamie", "email": "jamie@example.test"}
    ]


def test_text_body_joins_text_parts():
    text = _text_body({
        "textBody": [{"partId": "a"}, {"partId": "b"}],
        "bodyValues": {
            "a": {"value": "first"},
            "b": {"value": "second"},
        },
    })
    assert text == "first\n\nsecond"


def test_folder_resolution_uses_oliver_children():
    client = FakeJMAPClient()
    assert client.folders.inbox_oliver == "inbox-oliver"
    assert client.folders.sent_oliver == "sent-oliver"
    assert client.folders.drafts == "drafts"


def test_folder_resolution_errors_without_oliver_child():
    rows = [
        {"id": "inbox", "name": "Inbox", "parentId": None, "role": "inbox"},
    ]
    try:
        JMAPClient._find_child(rows, {"inbox": rows[0]}, "inbox", "Oliver", "Inbox/Oliver")
    except JMAPError as e:
        assert "Inbox/Oliver" in str(e)
    else:
        raise AssertionError("expected JMAPError")


def test_unread_oliver_email_queries_unread_in_folder():
    client = FakeJMAPClient()
    messages = client.unread_oliver_email(limit=3)
    assert messages == [
        InboundEmail(
            id="m1",
            thread_id="t1",
            message_id="msg1@example.test",
            from_name="Jamie",
            from_email="jamie@example.test",
            to=["oliver@rwbookclub.com"],
            cc=[],
            reply_to=[],
            subject="Question",
            text="Hello Oliver",
            received_at="2026-06-09T12:00:00Z",
            references=["prior@example.test"],
        )
    ]
    query = client.calls[-1][0][0][1]
    assert query["filter"] == {"inMailbox": "inbox-oliver", "notKeyword": "$seen"}


def test_send_email_creates_draft_submits_and_moves_to_oliver_sent():
    client = FakeJMAPClient()
    result = client.send_email(
        to=["Jamie <jamie@example.test>"],
        subject="Verification",
        body="It works.",
        in_reply_to="msg1@example.test",
    )
    assert result["submissionId"] == "submission1"
    calls = client.calls[-1][0]
    draft = calls[0][1]["create"]["oliverDraft"]
    submit = calls[1][1]["create"]["oliverSend"]
    on_success = calls[1][1]["onSuccessUpdateEmail"]["#oliverSend"]
    assert draft["mailboxIds"] == {"drafts": True}
    assert draft["to"] == [{"name": "Jamie", "email": "jamie@example.test"}]
    assert draft["inReplyTo"] == ["msg1@example.test"]
    assert draft["bodyStructure"]["type"] == "multipart/alternative"
    assert draft["bodyValues"]["text"]["value"] == "It works."
    assert "It works." in draft["bodyValues"]["html"]["value"]
    assert submit["identityId"] == "identity-oliver"
    assert submit["emailId"] == "#oliverDraft"
    assert on_success["mailboxIds/drafts"] is None
    assert on_success["mailboxIds/sent-oliver"] is True


def test_send_email_accepts_explicit_html_body():
    client = FakeJMAPClient()
    client.send_email(
        to=["jamie@example.test"],
        subject="HTML",
        body="Plain",
        html_body="<p>HTML</p>",
    )
    draft = client.calls[-1][0][0][1]["create"]["oliverDraft"]
    assert draft["bodyValues"]["html"]["value"] == "<p>HTML</p>"
