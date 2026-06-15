"""Fastmail JMAP mail client for Oliver.

The shared mailbox contains folders for multiple agents. Oliver only reads from
Inbox/Oliver and stores sent mail in Sent/Oliver; both names are configurable in
agent.config, but the isolation rule lives here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from email.utils import getaddresses
from functools import cached_property
from typing import Any

import requests

from agent import config, email_tracking

log = logging.getLogger("oliver.email")

CORE = "urn:ietf:params:jmap:core"
MAIL = "urn:ietf:params:jmap:mail"
SUBMISSION = "urn:ietf:params:jmap:submission"


class JMAPError(RuntimeError):
    """Raised for missing config, malformed JMAP state, or server-side errors."""


@dataclass(frozen=True)
class InboundEmail:
    id: str
    thread_id: str | None
    message_id: str | None
    from_name: str | None
    from_email: str
    subject: str
    text: str
    received_at: str | None
    references: list[str]

    @property
    def speaker(self) -> str:
        return self.from_name or self.from_email

    @property
    def reply_subject(self) -> str:
        subject = self.subject.strip() or "(no subject)"
        return subject if subject.lower().startswith("re:") else f"Re: {subject}"


@dataclass(frozen=True)
class MailFolders:
    inbox_oliver: str
    sent_oliver: str
    drafts: str


def enabled() -> bool:
    return bool(config.FASTMAIL_JMAP_TOKEN)


class JMAPClient:
    def __init__(self, *, token: str | None = None, session_url: str | None = None,
                 timeout: float = 30.0) -> None:
        self.token = token or config.FASTMAIL_JMAP_TOKEN
        self.session_url = session_url or config.FASTMAIL_JMAP_SESSION_URL
        self.timeout = timeout
        if not self.token:
            raise JMAPError("FASTMAIL_JMAP_TOKEN is not configured")

    @cached_property
    def session(self) -> dict[str, Any]:
        r = requests.get(
            self.session_url,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=self.timeout,
        )
        self._raise_for_response(r)
        return r.json()

    @property
    def api_url(self) -> str:
        return self.session["apiUrl"]

    @property
    def mail_account_id(self) -> str:
        return self._account_id(MAIL)

    @property
    def submission_account_id(self) -> str:
        return self._account_id(SUBMISSION)

    def _account_id(self, capability: str) -> str:
        primary = self.session.get("primaryAccounts", {})
        if primary.get(capability):
            return primary[capability]
        accounts = self.session.get("accounts", {})
        for account_id, account in accounts.items():
            if capability in account.get("accountCapabilities", {}):
                return account_id
        raise JMAPError(f"No account supports {capability}")

    def call(self, method_calls: list[list[Any]], *, using: list[str] | None = None) -> list[list[Any]]:
        body = {"using": using or [CORE, MAIL, SUBMISSION], "methodCalls": method_calls}
        r = requests.post(
            self.api_url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=body,
            timeout=self.timeout,
        )
        self._raise_for_response(r)
        data = r.json()
        if data.get("methodResponses") is None:
            raise JMAPError(f"Malformed JMAP response: {data!r}")
        for method, payload, call_id in data["methodResponses"]:
            if method == "error":
                raise JMAPError(f"JMAP call {call_id} failed: {payload}")
        return data["methodResponses"]

    @staticmethod
    def _raise_for_response(response: requests.Response) -> None:
        try:
            response.raise_for_status()
        except requests.HTTPError as e:
            text = response.text[:500]
            raise JMAPError(f"Fastmail HTTP {response.status_code}: {text}") from e

    @cached_property
    def folders(self) -> MailFolders:
        rows = self._mailboxes()
        by_id = {m["id"]: m for m in rows}
        inbox_parent = self._find_parent(rows, config.OLIVER_EMAIL_INBOX_PARENT, "inbox")
        sent_parent = self._find_parent(rows, config.OLIVER_EMAIL_SENT_PARENT, "sent")
        drafts = self._find_parent(rows, "Drafts", "drafts")
        inbox_oliver = self._find_child(
            rows, by_id, inbox_parent["id"], config.OLIVER_EMAIL_INBOX_FOLDER,
            f"{config.OLIVER_EMAIL_INBOX_PARENT}/{config.OLIVER_EMAIL_INBOX_FOLDER}",
        )
        sent_oliver = self._find_child(
            rows, by_id, sent_parent["id"], config.OLIVER_EMAIL_SENT_FOLDER,
            f"{config.OLIVER_EMAIL_SENT_PARENT}/{config.OLIVER_EMAIL_SENT_FOLDER}",
        )
        return MailFolders(
            inbox_oliver=inbox_oliver["id"],
            sent_oliver=sent_oliver["id"],
            drafts=drafts["id"],
        )

    def _mailboxes(self) -> list[dict[str, Any]]:
        responses = self.call([
            ["Mailbox/get", {
                "accountId": self.mail_account_id,
                "ids": None,
                "properties": ["id", "name", "parentId", "role"],
            }, "mailboxes"],
        ], using=[CORE, MAIL])
        return responses[0][1]["list"]

    @staticmethod
    def _find_parent(rows: list[dict[str, Any]], name: str, role: str) -> dict[str, Any]:
        role_match = next((m for m in rows if m.get("role") == role), None)
        if role_match:
            return role_match
        name_match = next((m for m in rows if m.get("parentId") is None and m.get("name") == name), None)
        if name_match:
            return name_match
        raise JMAPError(f"Could not find {name} mailbox")

    @staticmethod
    def _find_child(rows: list[dict[str, Any]], by_id: dict[str, dict[str, Any]],
                    parent_id: str, name: str, path: str) -> dict[str, Any]:
        child = next((m for m in rows if m.get("parentId") == parent_id and m.get("name") == name), None)
        if child:
            return child
        flat = next((m for m in rows if m.get("name") == path), None)
        if flat and (flat.get("parentId") is None or flat.get("parentId") in by_id):
            return flat
        raise JMAPError(f"Could not find mailbox {path}")

    @cached_property
    def identity_id(self) -> str:
        responses = self.call([
            ["Identity/get", {
                "accountId": self.submission_account_id,
                "ids": None,
                "properties": ["id", "name", "email"],
            }, "identities"],
        ], using=[CORE, SUBMISSION])
        identities = responses[0][1]["list"]
        wanted = config.OLIVER_EMAIL_ADDRESS.lower()
        match = next((i for i in identities if (i.get("email") or "").lower() == wanted), None)
        if not match and identities:
            log.warning("No identity for %s; using first configured Fastmail identity", wanted)
            match = identities[0]
        if not match:
            raise JMAPError("No JMAP sending identities are configured")
        return match["id"]

    def unread_oliver_email(self, *, limit: int | None = None) -> list[InboundEmail]:
        limit = limit or config.OLIVER_EMAIL_MAX_PER_POLL
        responses = self.call([
            ["Email/query", {
                "accountId": self.mail_account_id,
                "filter": {
                    "inMailbox": self.folders.inbox_oliver,
                    "notKeyword": "$seen",
                },
                "sort": [{"property": "receivedAt", "isAscending": True}],
                "limit": max(1, min(limit, 20)),
            }, "query"],
            ["Email/get", {
                "accountId": self.mail_account_id,
                "#ids": {"resultOf": "query", "name": "Email/query", "path": "/ids"},
                "properties": [
                    "id", "threadId", "messageId", "from", "subject", "receivedAt",
                    "textBody", "bodyValues", "references",
                ],
                "fetchTextBodyValues": True,
                "maxBodyValueBytes": 20000,
            }, "get"],
        ], using=[CORE, MAIL])
        emails = responses[1][1].get("list") or []
        return [self._inbound_from_jmap(e) for e in emails if self._sender_email(e)]

    def mark_seen(self, email_id: str, *, answered: bool = False) -> None:
        update = {"keywords/$seen": True}
        if answered:
            update["keywords/$answered"] = True
        self.call([
            ["Email/set", {
                "accountId": self.mail_account_id,
                "update": {email_id: update},
            }, "mark"],
        ], using=[CORE, MAIL])

    def send_email(self, *, to: list[str] | str, subject: str, body: str,
                   html_body: str | None = None,
                   cc: list[str] | str | None = None, bcc: list[str] | str | None = None,
                   in_reply_to: str | None = None, references: list[str] | None = None) -> dict[str, Any]:
        recipients = _addresses(to)
        if not recipients:
            raise JMAPError("At least one recipient is required")
        cc_recipients = _addresses(cc)
        bcc_recipients = _addresses(bcc)
        create_id = "oliverDraft"
        submit_id = "oliverSend"
        if html_body is None and config.OLIVER_EMAIL_HTML_ENABLED:
            html_body = email_tracking.text_to_html(body)
        if html_body:
            body_structure = {
                "type": "multipart/alternative",
                "subParts": [
                    {"type": "text/plain", "partId": "text"},
                    {"type": "text/html", "partId": "html"},
                ],
            }
            body_values = {
                "text": {"value": body, "isTruncated": False},
                "html": {"value": html_body, "isTruncated": False},
            }
        else:
            body_structure = {"type": "text/plain", "partId": "text"}
            body_values = {"text": {"value": body, "isTruncated": False}}
        email_obj: dict[str, Any] = {
            "mailboxIds": {self.folders.drafts: True},
            "keywords": {"$draft": True, "$seen": True},
            "from": [{"name": "Oliver", "email": config.OLIVER_EMAIL_ADDRESS}],
            "to": recipients,
            "subject": subject,
            "bodyStructure": body_structure,
            "bodyValues": body_values,
        }
        if cc_recipients:
            email_obj["cc"] = cc_recipients
        if bcc_recipients:
            email_obj["bcc"] = bcc_recipients
        refs = [r for r in (references or []) if r]
        if in_reply_to:
            email_obj["inReplyTo"] = [in_reply_to]
            if in_reply_to not in refs:
                refs.append(in_reply_to)
        if refs:
            email_obj["references"] = refs[-20:]

        rcpt_to = [{"email": r["email"], "parameters": None} for r in recipients + cc_recipients + bcc_recipients]
        responses = self.call([
            ["Email/set", {
                "accountId": self.mail_account_id,
                "create": {create_id: email_obj},
            }, "create"],
            ["EmailSubmission/set", {
                "accountId": self.submission_account_id,
                "create": {
                    submit_id: {
                        "identityId": self.identity_id,
                        "emailId": f"#{create_id}",
                        "envelope": {
                            "mailFrom": {"email": config.OLIVER_EMAIL_ADDRESS, "parameters": None},
                            "rcptTo": rcpt_to,
                        },
                    },
                },
                "onSuccessUpdateEmail": {
                    f"#{submit_id}": {
                        f"mailboxIds/{self.folders.drafts}": None,
                        f"mailboxIds/{self.folders.sent_oliver}": True,
                        "keywords/$draft": None,
                        "keywords/$seen": True,
                    },
                },
            }, "submit"],
        ])
        create_payload = responses[0][1]
        submit_payload = responses[1][1]
        if create_payload.get("notCreated"):
            raise JMAPError(f"Email draft was not created: {create_payload['notCreated']}")
        if submit_payload.get("notCreated"):
            raise JMAPError(f"Email was not submitted: {submit_payload['notCreated']}")
        created_email = (create_payload.get("created") or {}).get(create_id) or {}
        created_submission = (submit_payload.get("created") or {}).get(submit_id) or {}
        return {
            "emailId": created_email.get("id"),
            "threadId": created_email.get("threadId"),
            "submissionId": created_submission.get("id"),
            "to": [r["email"] for r in recipients],
            "cc": [r["email"] for r in cc_recipients],
            "bccCount": len(bcc_recipients),
            "subject": subject,
        }

    @staticmethod
    def _sender_email(email_obj: dict[str, Any]) -> str | None:
        froms = email_obj.get("from") or []
        return (froms[0].get("email") if froms else None) or None

    def _inbound_from_jmap(self, email_obj: dict[str, Any]) -> InboundEmail:
        froms = email_obj.get("from") or [{}]
        sender = froms[0]
        return InboundEmail(
            id=email_obj["id"],
            thread_id=email_obj.get("threadId"),
            message_id=(email_obj.get("messageId") or [None])[0],
            from_name=sender.get("name"),
            from_email=sender.get("email") or "",
            subject=email_obj.get("subject") or "",
            text=_text_body(email_obj),
            received_at=email_obj.get("receivedAt"),
            references=email_obj.get("references") or [],
        )


_client: JMAPClient | None = None


def client() -> JMAPClient:
    global _client
    if _client is None:
        _client = JMAPClient()
    return _client


def send_email(**kwargs) -> dict[str, Any]:
    return client().send_email(**kwargs)


def unread_oliver_email(*, limit: int | None = None) -> list[InboundEmail]:
    return client().unread_oliver_email(limit=limit)


def mark_seen(email_id: str, *, answered: bool = False) -> None:
    client().mark_seen(email_id, answered=answered)


def _addresses(value: list[str] | str | None) -> list[dict[str, str | None]]:
    if value is None:
        return []
    raw = value if isinstance(value, list) else [value]
    parsed = getaddresses(raw)
    out = []
    seen = set()
    for name, email in parsed:
        email = email.strip().lower()
        if not email or "@" not in email or email in seen:
            continue
        seen.add(email)
        out.append({"name": name or None, "email": email})
    return out


def _text_body(email_obj: dict[str, Any]) -> str:
    body_values = email_obj.get("bodyValues") or {}
    parts = email_obj.get("textBody") or []
    chunks: list[str] = []
    for part in parts:
        part_id = part.get("partId")
        if part_id and part_id in body_values:
            value = body_values[part_id].get("value") or ""
            if value:
                chunks.append(value)
    return "\n\n".join(chunks).strip() or (email_obj.get("preview") or "").strip()
