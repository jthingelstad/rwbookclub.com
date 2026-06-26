"""Deterministic safety policy for Oliver's email surface."""

from __future__ import annotations

import re
from html import unescape
from dataclasses import dataclass
from email.utils import getaddresses

from agent import config, corpus_read as cr, db

EMAIL_QUOTE_RE = re.compile(r"^(>|on .+wrote:|from:|sent:|to:|subject:|--\s*$)", re.IGNORECASE)


@dataclass(frozen=True)
class InboundDecision:
    allowed: bool
    reason: str
    member_slug: str | None = None
    reply_to: list[str] | None = None
    is_mailing_list: bool = False


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def configured_mailing_list_address() -> str:
    return normalize_email(config.BOOK_CLUB_MAILING_LIST_ADDRESS)


def is_mailing_list_address(email: str | None) -> bool:
    return normalize_email(email) == configured_mailing_list_address()


def parse_addresses(values: list[str] | str | None) -> list[str]:
    if values is None:
        return []
    raw = values if isinstance(values, list) else [values]
    out: list[str] = []
    seen: set[str] = set()
    for _name, email in getaddresses(raw):
        normalized = normalize_email(email)
        if not normalized or "@" not in normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def known_member_slug_for_email(email: str | None) -> str | None:
    return db.member_slug_for_email(normalize_email(email))


def known_member_slug_for_display_name(name: str | None) -> str | None:
    if not name:
        return None
    cleaned = re.sub(r"(?i)\s+via\s+rwbookclub\s*$", "", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned.strip("'\"").strip())
    if not cleaned:
        return None
    member = cr.find_member(cleaned)
    if member:
        return member.get("slug")
    first_name = cleaned.split(" ", 1)[0]
    matches = [
        m for m in cr.members()
        if (m.get("name") or "").strip().lower() == first_name.lower()
    ]
    if len(matches) == 1:
        return matches[0].get("slug")
    return None


def is_known_member_address(email: str | None) -> bool:
    return known_member_slug_for_email(email) is not None


def is_mailing_list_message(msg) -> bool:
    addresses = [getattr(msg, "from_email", "")]
    addresses.extend(getattr(msg, "to", []) or [])
    addresses.extend(getattr(msg, "cc", []) or [])
    addresses.extend(getattr(msg, "reply_to", []) or [])
    return any(is_mailing_list_address(address) for address in addresses)


def _plain_visible_text(text: str) -> str:
    text = re.sub(r"(?is)<blockquote\b.*?</blockquote>", "\n", text or "")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(?:p|div|li|tr|h[1-6])>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    return unescape(text)


def current_message_text(text: str) -> str:
    lines: list[str] = []
    for line in _plain_visible_text(text).splitlines():
        if EMAIL_QUOTE_RE.match(line.strip()):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def inbound_decision(msg) -> InboundDecision:
    member_slug = known_member_slug_for_email(getattr(msg, "from_email", None))
    list_message = is_mailing_list_message(msg)
    if list_message and not member_slug:
        member_slug = known_member_slug_for_display_name(getattr(msg, "from_name", None))
    if list_message:
        return InboundDecision(
            allowed=True,
            reason="mailing_list_candidate",
            member_slug=member_slug,
            reply_to=[configured_mailing_list_address()],
            is_mailing_list=True,
        )
    if member_slug:
        return InboundDecision(
            allowed=True,
            reason="known_member",
            member_slug=member_slug,
            reply_to=[normalize_email(getattr(msg, "from_email", None))],
        )
    return InboundDecision(allowed=False, reason="sender_not_allowed")


def validate_model_email_recipients(*, to: list[str] | str, cc: list[str] | str | None = None) -> str | None:
    recipients = parse_addresses(to) + parse_addresses(cc)
    if not recipients:
        return "at least one recipient email address is required"
    mailing_list = configured_mailing_list_address()
    if mailing_list and any(address == mailing_list for address in recipients):
        return (
            "the book club mailing list can only be emailed by approved meeting-cadence paths, "
            "not the general send_email tool"
        )
    unknown = [address for address in recipients if not is_known_member_address(address)]
    if unknown:
        return "Oliver can only email linked book club member addresses from this tool"
    return None
