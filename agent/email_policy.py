"""Deterministic safety policy for Oliver's email surface."""

from __future__ import annotations

import re
from dataclasses import dataclass
from email.utils import getaddresses

from agent import config, db

EMAIL_QUOTE_RE = re.compile(r"^(>|on .+wrote:|from:|sent:|to:|subject:|--\s*$)", re.IGNORECASE)
OLIVER_REFERENCE_RE = re.compile(r"\boliver\b|oliver@rwbookclub\.com", re.IGNORECASE)


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


def is_known_member_address(email: str | None) -> bool:
    return known_member_slug_for_email(email) is not None


def is_mailing_list_message(msg) -> bool:
    addresses = [getattr(msg, "from_email", "")]
    addresses.extend(getattr(msg, "to", []) or [])
    addresses.extend(getattr(msg, "cc", []) or [])
    addresses.extend(getattr(msg, "reply_to", []) or [])
    return any(is_mailing_list_address(address) for address in addresses)


def _unquoted_text(text: str) -> str:
    lines: list[str] = []
    for line in (text or "").splitlines():
        if EMAIL_QUOTE_RE.match(line.strip()):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def mailing_list_message_warrants_reply(subject: str, body: str) -> bool:
    visible = f"{subject or ''}\n{_unquoted_text(body)}"
    return bool(OLIVER_REFERENCE_RE.search(visible) or "?" in visible)


def inbound_decision(msg) -> InboundDecision:
    member_slug = known_member_slug_for_email(getattr(msg, "from_email", None))
    list_message = is_mailing_list_message(msg)
    if list_message:
        if not mailing_list_message_warrants_reply(getattr(msg, "subject", ""), getattr(msg, "text", "")):
            return InboundDecision(
                allowed=False,
                reason="mailing_list_not_addressed",
                member_slug=member_slug,
                is_mailing_list=True,
            )
        return InboundDecision(
            allowed=True,
            reason="mailing_list_addressed",
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
