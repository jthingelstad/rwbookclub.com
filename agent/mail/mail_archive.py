"""Mailing-list archive normalization and import for Oliver.

The archive stores message bodies and searchable metadata. It deliberately does
not store attachment blobs or extracted attachment text in v1.
"""

from __future__ import annotations

import argparse
import email.utils
import hashlib
import mailbox
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import timezone
from email.header import decode_header, make_header
from html import unescape
from pathlib import Path
from typing import Iterable

from agent import config, db
from agent.mail import email_policy

GOOGLE_GROUPS_ADDRESS = "rwbookclub@googlegroups.com"
GOOGLE_GROUPS_FOOTER = "You received this message because you are subscribed to the Google Groups"
LIST_FOOTER_RE = re.compile(
    r"(?is)\n--\s*\nYou received this message because you are subscribed to the Google Groups.*$"
)
QUOTE_START_RE = re.compile(
    r"^\s*(>|On .+|From:|Sent:|To:|Subject:|-{2,}\s*Original Message\s*-{2,})",
    re.IGNORECASE,
)
SUBJECT_PREFIX_RE = re.compile(r"^\s*(?:re|fw|fwd)\s*:\s*", re.IGNORECASE)
RW_PREFIX_RE = re.compile(r"^\s*\[rwbookclub\]\s*", re.IGNORECASE)

ARCHIVE_ALIASES = {
    "terveen@cs.umn.edu": "loren",
    "erik@erik.jordan.name": "erik",
    "jthingelstad@gmail.com": "jamie",
    "snowfall@acm.org": "tom",
}


@dataclass
class ImportReport:
    total: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    threads: set[str] = field(default_factory=set)
    senders: Counter[str] = field(default_factory=Counter)
    resolved: Counter[str] = field(default_factory=Counter)
    unresolved: Counter[str] = field(default_factory=Counter)
    html_only: int = 0
    attachments_by_type: Counter[str] = field(default_factory=Counter)
    missing_message_id: int = 0
    missing_date: int = 0
    first_sent_at: str | None = None
    last_sent_at: str | None = None

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped": self.skipped,
            "threads": len(self.threads),
            "senders": dict(self.senders),
            "resolved": dict(self.resolved),
            "unresolved": dict(self.unresolved),
            "html_only": self.html_only,
            "attachmentsByType": dict(self.attachments_by_type),
            "missingMessageId": self.missing_message_id,
            "missingDate": self.missing_date,
            "firstSentAt": self.first_sent_at,
            "lastSentAt": self.last_sent_at,
        }


def seed_archive_aliases() -> None:
    for email, slug in ARCHIVE_ALIASES.items():
        db.link_member_email(email, slug, linked_by="mail-archive-import")


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:  # noqa: BLE001 - malformed historical headers should not abort import
        return str(value).strip()


def _addresses(value: str | list[str] | None) -> list[dict[str, str | None]]:
    raw = value if isinstance(value, list) else [value or ""]
    rows: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for name, address in email.utils.getaddresses([_decode_header(v) for v in raw]):
        normalized = normalize_email(address)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        rows.append({"name": name or None, "email": normalized})
    return rows


def normalize_email(address: str | None) -> str:
    return (address or "").strip().lower()


def normalize_subject(subject: str | None) -> str:
    out = _decode_header(subject)
    while True:
        cleaned = SUBJECT_PREFIX_RE.sub("", out)
        if cleaned == out:
            break
        out = cleaned
    out = RW_PREFIX_RE.sub("", out)
    return re.sub(r"\s+", " ", out).strip().lower()


def member_slug_for_sender(from_email: str | None, from_name: str | None = None) -> str | None:
    linked = db.member_slug_for_email(from_email)
    if linked:
        return linked
    if from_name:
        return email_policy.known_member_slug_for_display_name(from_name)
    return None


def _parsed_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _decode_part(part) -> str:
    charset = part.get_content_charset()
    try:
        payload = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001
        payload = None
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    try:
        return payload.decode(charset or "utf-8", "replace")
    except LookupError:
        return payload.decode("utf-8", "replace")


def _html_to_text(value: str) -> str:
    text = re.sub(r"(?is)<blockquote\b.*?</blockquote>", "\n", value or "")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(?:p|div|li|tr|h[1-6])>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"[ \t]+", " ", text)


def _body_parts(msg) -> tuple[str, str, list[dict]]:
    plain: list[str] = []
    html: list[str] = []
    attachments: list[dict] = []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        disposition = _decode_header(part.get("Content-Disposition"))
        disposition_lower = disposition.lower()
        filename = _decode_header(part.get_filename()) if part.get_filename() else None
        is_attachment = bool(
            filename
            or "attachment" in disposition_lower
            or ("inline" in disposition_lower and not content_type.startswith("text/"))
        )
        if is_attachment:
            size = 0
            try:
                payload = part.get_payload(decode=True) or b""
                size = len(payload)
            except Exception:  # noqa: BLE001
                pass
            attachments.append({
                "contentType": content_type,
                "filename": filename,
                "contentId": _decode_header(part.get("Content-ID")) or None,
                "disposition": disposition or None,
                "sizeBytes": size,
            })
            continue
        if content_type == "text/plain":
            plain.append(_decode_part(part))
        elif content_type == "text/html":
            html.append(_decode_part(part))
    return "\n".join(plain).strip(), "\n".join(html).strip(), attachments


def clean_body(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if re.search(r"(?is)<(?:br|div|p|blockquote|span|html|body)\b", text):
        text = _html_to_text(text)
    text = LIST_FOOTER_RE.sub("", text)
    if GOOGLE_GROUPS_FOOTER in text:
        text = text.split(GOOGLE_GROUPS_FOOTER, 1)[0]
    lines: list[str] = []
    blank_pending = False
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if QUOTE_START_RE.match(stripped):
            break
        if not stripped:
            blank_pending = bool(lines)
            continue
        if blank_pending:
            lines.append("")
            blank_pending = False
        lines.append(line)
    cleaned = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", cleaned)


def _message_id_or_hash(msg, body_text: str) -> tuple[str, bool]:
    message_id = _decode_header(msg.get("Message-ID") or msg.get("Message-Id"))
    if message_id:
        return message_id, False
    seed = "\n".join([
        _decode_header(msg.get("Date")),
        _decode_header(msg.get("From")),
        _decode_header(msg.get("Subject")),
        body_text,
    ])
    return "sha256:" + hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest(), True


def _parent_message_id(msg) -> str | None:
    in_reply_to = _decode_header(msg.get("In-Reply-To"))
    if in_reply_to:
        match = re.search(r"<[^>]+>", in_reply_to)
        return match.group(0) if match else in_reply_to
    references = _decode_header(msg.get("References"))
    refs = re.findall(r"<[^>]+>", references)
    return refs[-1] if refs else None


def _list_id(msg, to_rows: list[dict], cc_rows: list[dict]) -> str | None:
    been_there = _decode_header(msg.get("X-BeenThere")).lower()
    if GOOGLE_GROUPS_ADDRESS in been_there:
        return GOOGLE_GROUPS_ADDRESS
    addresses = {r["email"] for r in to_rows + cc_rows}
    if GOOGLE_GROUPS_ADDRESS in addresses:
        return GOOGLE_GROUPS_ADDRESS
    configured = normalize_email(config.BOOK_CLUB_MAILING_LIST_ADDRESS)
    return configured if configured and configured in addresses else None


def _headers(msg) -> dict:
    keep = [
        "Message-ID", "Message-Id", "Date", "From", "To", "Cc", "Reply-To",
        "Subject", "X-GM-THRID", "X-GM-MSGID", "X-BeenThere", "References",
        "In-Reply-To", "Content-Type",
    ]
    return {h: _decode_header(msg.get(h)) for h in keep if msg.get(h) is not None}


def normalized_from_mbox_message(msg, *, source_ref: str) -> tuple[dict, dict]:
    from_name, from_email = email.utils.parseaddr(_decode_header(msg.get("From")))
    from_email = normalize_email(from_email)
    to_rows = _addresses(msg.get_all("To") or msg.get("To"))
    cc_rows = _addresses(msg.get_all("Cc") or msg.get("Cc"))
    reply_to_rows = _addresses(msg.get_all("Reply-To") or msg.get("Reply-To"))
    body_plain, body_html, attachments = _body_parts(msg)
    body_text = body_plain or _html_to_text(body_html)
    body_clean = clean_body(body_text)
    message_id, missing_message_id = _message_id_or_hash(msg, body_text)
    gmail_thread = _decode_header(msg.get("X-GM-THRID"))
    thread_id = f"x-gm-thrid:{gmail_thread}" if gmail_thread else f"subject:{normalize_subject(msg.get('Subject'))}"
    sent_at = _parsed_date(msg.get("Date"))
    member_slug = member_slug_for_sender(from_email, from_name)
    list_id = _list_id(msg, to_rows, cc_rows)
    normalized = {
        "message_id": message_id,
        "thread_id": thread_id,
        "parent_message_id": _parent_message_id(msg),
        "source": "historical_import",
        "source_ref": source_ref,
        "list_id": list_id,
        "from_email": from_email,
        "from_name": from_name or None,
        "member_slug": member_slug,
        "to": to_rows,
        "cc": cc_rows,
        "reply_to": reply_to_rows,
        "subject": _decode_header(msg.get("Subject")),
        "subject_normalized": normalize_subject(msg.get("Subject")),
        "sent_at": sent_at,
        "received_at": None,
        "body_text": body_text,
        "body_clean": body_clean,
        "body_html": None,
        "attachments": attachments or None,
        "headers": _headers(msg),
        "processed_inbound_email_id": None,
    }
    stats = {
        "missing_message_id": missing_message_id,
        "missing_date": sent_at is None,
        "html_only": bool(body_html and not body_plain),
        "attachments": attachments,
    }
    return normalized, stats


def normalized_from_inbound_email(msg, *, is_mailing_list: bool,
                                  member_slug: str | None = None) -> dict:
    to_rows = [{"name": None, "email": normalize_email(a)} for a in (msg.to or [])]
    cc_rows = [{"name": None, "email": normalize_email(a)} for a in (msg.cc or [])]
    reply_to_rows = [{"name": None, "email": normalize_email(a)} for a in (msg.reply_to or [])]
    message_id = msg.message_id or f"jmap:{msg.id}"
    list_id = config.BOOK_CLUB_MAILING_LIST_ADDRESS.lower() if is_mailing_list else None
    thread_id = (
        f"jmap:{msg.thread_id}" if msg.thread_id
        else f"email:{list_id or normalize_email(msg.from_email)}:{normalize_subject(msg.subject)}"
    )
    resolved_slug = member_slug if member_slug is not None else member_slug_for_sender(
        msg.from_email, msg.from_name,
    )
    return {
        "message_id": message_id,
        "thread_id": thread_id,
        "parent_message_id": msg.references[-1] if msg.references else None,
        "source": "live_jmap",
        "source_ref": msg.id,
        "list_id": list_id,
        "from_email": normalize_email(msg.from_email),
        "from_name": msg.from_name,
        "member_slug": resolved_slug,
        "to": to_rows,
        "cc": cc_rows,
        "reply_to": reply_to_rows,
        "subject": msg.subject or "",
        "subject_normalized": normalize_subject(msg.subject),
        "sent_at": msg.received_at,
        "received_at": msg.received_at,
        "body_text": msg.text or "",
        "body_clean": clean_body(msg.text or ""),
        "body_html": None,
        "attachments": None,
        "headers": {
            "Message-ID": msg.message_id,
            "References": msg.references,
        },
        "processed_inbound_email_id": msg.id,
    }


def archive_inbound_email(msg, *, is_mailing_list: bool, member_slug: str | None = None) -> bool:
    normalized = normalized_from_inbound_email(
        msg, is_mailing_list=is_mailing_list, member_slug=member_slug,
    )
    inserted = db.upsert_mail_message(normalized)
    db.rebuild_mail_thread_stats()
    return inserted


def import_mbox(path: str | Path, *, write: bool = False) -> ImportReport:
    path = Path(path)
    if write:
        seed_archive_aliases()
    report = ImportReport()
    for idx, msg in enumerate(mailbox.mbox(path), start=1):
        report.total += 1
        normalized, stats = normalized_from_mbox_message(
            msg, source_ref=f"{path}:{idx}",
        )
        report.threads.add(normalized["thread_id"])
        sender = normalized.get("from_email") or "(missing)"
        report.senders[sender] += 1
        if normalized.get("member_slug"):
            report.resolved[normalized["member_slug"]] += 1
        else:
            report.unresolved[sender] += 1
        if stats["missing_message_id"]:
            report.missing_message_id += 1
        if stats["missing_date"]:
            report.missing_date += 1
        if stats["html_only"]:
            report.html_only += 1
        for attachment in stats["attachments"]:
            report.attachments_by_type[attachment["contentType"] or "unknown"] += 1
        sent_at = normalized.get("sent_at")
        if sent_at:
            report.first_sent_at = min(report.first_sent_at, sent_at) if report.first_sent_at else sent_at
            report.last_sent_at = max(report.last_sent_at, sent_at) if report.last_sent_at else sent_at
        if write:
            inserted = db.upsert_mail_message(normalized)
            if inserted:
                report.inserted += 1
            else:
                report.updated += 1
            if not normalized.get("member_slug") and normalized.get("from_email"):
                db.add_identity_claim(
                    surface="email",
                    identifier=normalized["from_email"],
                    display_name=normalized.get("from_name"),
                    evidence={
                        "source": "mail_archive_import",
                        "message_id": normalized["message_id"],
                        "subject": normalized.get("subject"),
                    },
                )
        else:
            report.skipped += 1
    if write:
        db.rebuild_mail_thread_stats()
    return report


def _main() -> None:
    parser = argparse.ArgumentParser(description="Import Oliver mailing-list mbox archive.")
    parser.add_argument("mbox", help="Path to mbox file")
    parser.add_argument("--write", action="store_true", help="Write to Oliver's SQLite database")
    args = parser.parse_args()
    report = import_mbox(args.mbox, write=args.write)
    for key, value in report.as_dict().items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    _main()
