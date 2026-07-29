"""Build a private, read-only evidence bundle for Oliver's external Evaluator.

The bundle combines exact Discord interaction pairs, surrounding stored Discord
turns, email received by Oliver, email replies archived by Oliver, and every
outbound email/Discord post in the durable outbox. Raw output is permitted only
under the gitignored ``AGENT-TEAM/notes/evaluator`` root.

This module deliberately imports no ``agent`` package: importing Oliver runtime
modules can initialize writers or expose delivery paths that an evaluator does
not need.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:  # Support both ``python scripts/...`` and module/test imports.
    from scripts.read_only_db import connect_read_only
except ModuleNotFoundError:  # pragma: no cover - direct script fallback
    from read_only_db import connect_read_only

ROOT = Path(__file__).resolve().parent.parent
RAW_RESULTS_ROOT = ROOT / "AGENT-TEAM" / "notes" / "evaluator"
REQUIRED_TABLES = {
    "activity_events",
    "conversations",
    "events",
    "feedback",
    "inbound_emails",
    "job_runs",
    "mail_messages",
    "outbox_messages",
    "responses",
    "review_drafts",
}


class EvidenceError(RuntimeError):
    """The production evidence contract is missing or cannot be normalized safely."""


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _in_window(value: str | None, since: datetime, until: datetime) -> bool:
    parsed = _parse_time(value)
    return bool(parsed and since <= parsed <= until)


def _json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except TypeError, ValueError:
        return default


def _addresses(value: Any) -> list[str]:
    """Normalize the few address shapes stored by archive and outbox payloads."""
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped[0] in '[{"':
            decoded = _json(stripped, None)
            if decoded is not None:
                return _addresses(decoded)
        return [stripped.lower()]
    if isinstance(value, dict):
        address = value.get("email") or value.get("address")
        return [str(address).strip().lower()] if address else []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            out.extend(_addresses(item))
        return out
    return []


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
    }


def _assert_contract(conn: sqlite3.Connection) -> None:
    missing = sorted(REQUIRED_TABLES - _tables(conn))
    if missing:
        raise EvidenceError(f"Oliver evidence tables are missing: {', '.join(missing)}")


def _message(
    *,
    evidence_id: str,
    kind: str,
    surface: str,
    direction: str,
    context_id: str | None,
    occurred_at: str | None,
    body: str | None,
    subject: str | None = None,
    delivery_status: str | None = None,
    visibility: str,
    links: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "evidence_id": evidence_id,
        "kind": kind,
        "surface": surface,
        "direction": direction,
        "context_id": context_id,
        "occurred_at": occurred_at,
        "body": body,
        "subject": subject,
        "delivery_status": delivery_status,
        "visibility": visibility,
        "links": links or {},
        "metadata": metadata or {},
    }


def _discord_evidence(
    conn: sqlite3.Connection, since: datetime, until: datetime
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    messages: list[dict[str, Any]] = []
    interactions: list[dict[str, Any]] = []
    context: list[dict[str, Any]] = []

    feedback: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute(
        "SELECT message_id, user_name, reaction, created_at FROM feedback ORDER BY id"
    ):
        feedback.setdefault(str(row["message_id"]), []).append(
            {
                "user_name": row["user_name"],
                "reaction": row["reaction"],
                "created_at": row["created_at"],
            }
        )

    for row in conn.execute(
        "SELECT message_id, channel_id, speaker, question, reply, created_at "
        "FROM responses ORDER BY created_at, message_id"
    ):
        if not _in_window(row["created_at"], since, until):
            continue
        response_id = str(row["message_id"])
        question_id = f"discord-question:{response_id}"
        reply_id = f"discord-reply:{response_id}"
        links = {"response_id": response_id}
        messages.extend(
            [
                _message(
                    evidence_id=question_id,
                    kind="discord_interaction",
                    surface="discord",
                    direction="member_to_oliver",
                    context_id=str(row["channel_id"]),
                    occurred_at=row["created_at"],
                    body=row["question"],
                    visibility="club_shared",
                    links={**links, "paired_evidence_id": reply_id},
                    metadata={"speaker": row["speaker"]},
                ),
                _message(
                    evidence_id=reply_id,
                    kind="discord_interaction",
                    surface="discord",
                    direction="oliver_to_club",
                    context_id=str(row["channel_id"]),
                    occurred_at=row["created_at"],
                    body=row["reply"],
                    visibility="club_shared",
                    links={**links, "paired_evidence_id": question_id},
                    metadata={"feedback": feedback.get(response_id, [])},
                ),
            ]
        )
        interactions.append(
            {
                "interaction_id": f"discord:{response_id}",
                "response_id": response_id,
                "channel_id": str(row["channel_id"]),
                "speaker": row["speaker"],
                "question_evidence_id": question_id,
                "reply_evidence_id": reply_id,
                "created_at": row["created_at"],
                "feedback": feedback.get(response_id, []),
            }
        )

    for row in conn.execute(
        "SELECT id, channel_id, role, speaker, content, member_slug, created_at "
        "FROM conversations ORDER BY id"
    ):
        channel_id = str(row["channel_id"])
        if channel_id.startswith("email:") or not _in_window(row["created_at"], since, until):
            continue
        context.append(
            {
                "context_id": f"discord-context:{row['id']}",
                "channel_id": channel_id,
                "role": row["role"],
                "speaker": row["speaker"],
                "member_slug": row["member_slug"],
                "body": row["content"],
                "created_at": row["created_at"],
            }
        )
    return messages, interactions, context


def _email_evidence(
    conn: sqlite3.Connection,
    since: datetime,
    until: datetime,
    *,
    oliver_email: str,
    mailing_list: str,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    archive_by_provider: dict[str, int] = {}
    archive_by_inbound: dict[str, int] = {}
    oliver_email = oliver_email.strip().lower()
    mailing_list = mailing_list.strip().lower()

    rows = conn.execute(
        "SELECT message_id, thread_id, parent_message_id, source, source_ref, list_id, "
        "from_email, from_name, member_id, to_json, cc_json, subject, sent_at, received_at, "
        "body_text, body_clean, imported_at, processed_inbound_email_id "
        "FROM mail_messages ORDER BY COALESCE(sent_at, received_at, imported_at), message_id"
    ).fetchall()
    for row in rows:
        occurred_at = row["sent_at"] or row["received_at"] or row["imported_at"]
        if not _in_window(occurred_at, since, until):
            continue
        recipients = _addresses(row["to_json"]) + _addresses(row["cc_json"])
        outbound = row["source"] == "live_jmap_outbound" or (
            oliver_email and str(row["from_email"] or "").strip().lower() == oliver_email
        )
        is_list = bool(row["list_id"]) or (mailing_list and mailing_list in recipients)
        surface = "mailing_list" if is_list else "direct_email"
        direction = (
            "oliver_to_club"
            if outbound and is_list
            else "oliver_to_member"
            if outbound
            else "member_to_oliver"
        )
        item = _message(
            evidence_id=f"mail:{row['message_id']}",
            kind="email_message",
            surface=surface,
            direction=direction,
            context_id=str(row["thread_id"]),
            occurred_at=occurred_at,
            body=row["body_clean"] or row["body_text"],
            subject=row["subject"],
            visibility="club_shared" if is_list else "member_private",
            links={
                "mail_message_id": row["message_id"],
                "parent_message_id": row["parent_message_id"],
            },
            metadata={
                "source": row["source"],
                "sender_name": row["from_name"],
                "sender_email": row["from_email"],
                "member_id": row["member_id"],
                "recipients": recipients,
            },
        )
        messages.append(item)
        index = len(messages) - 1
        provider_id = str(row["source_ref"] or "")
        if provider_id:
            archive_by_provider[provider_id] = index
        message_id = str(row["message_id"])
        if message_id.startswith("jmap-sent:"):
            archive_by_provider.setdefault(message_id.removeprefix("jmap-sent:"), index)
        inbound_id = str(row["processed_inbound_email_id"] or "")
        if inbound_id:
            archive_by_inbound[inbound_id] = index

    outbox_rows = conn.execute(
        "SELECT id, idempotency_key, payload_json, status, attempts, provider_ref_json, "
        "last_error, created_at, delivered_at FROM outbox_messages "
        "WHERE kind = 'email' ORDER BY created_at, id"
    ).fetchall()
    for row in outbox_rows:
        occurred_at = row["delivered_at"] or row["created_at"]
        if not _in_window(occurred_at, since, until):
            continue
        payload = _json(row["payload_json"], {})
        provider = _json(row["provider_ref_json"], {})
        provider_id = str(provider.get("emailId") or "")
        recipients = _addresses(payload.get("to")) + _addresses(payload.get("cc"))
        policy = str(payload.get("policy") or "trusted")
        is_list = policy == "cadence" or (mailing_list and mailing_list in recipients)
        index = archive_by_provider.get(provider_id) if provider_id else None
        outbox_metadata = {
            "outbox_id": row["id"],
            "idempotency_key": row["idempotency_key"],
            "attempts": row["attempts"],
            "last_error": row["last_error"],
            "policy": policy,
            "provider": provider,
            "recipients": recipients,
        }
        if index is not None:
            item = messages[index]
            item["body"] = payload.get("body") or item["body"]
            item["subject"] = payload.get("subject") or item["subject"]
            item["occurred_at"] = occurred_at
            item["delivery_status"] = row["status"]
            item["links"]["outbox_id"] = row["id"]
            item["metadata"]["outbox"] = outbox_metadata
            continue
        context_id = str(
            provider.get("threadId") or payload.get("in_reply_to") or row["idempotency_key"]
        )
        messages.append(
            _message(
                evidence_id=f"email-outbox:{row['id']}",
                kind="email_message",
                surface="mailing_list" if is_list else "direct_email",
                direction="oliver_to_club" if is_list else "oliver_to_member",
                context_id=context_id,
                occurred_at=occurred_at,
                body=payload.get("body"),
                subject=payload.get("subject"),
                delivery_status=row["status"],
                visibility="club_shared" if is_list else "member_private",
                links={"outbox_id": row["id"]},
                metadata={"outbox": outbox_metadata, "archive_match": False},
            )
        )

    for row in conn.execute(
        "SELECT email_id, thread_id, from_email, subject, status, reply_email_id, error, "
        "received_at, processed_at FROM inbound_emails ORDER BY processed_at, email_id"
    ):
        occurred_at = row["received_at"] or row["processed_at"]
        if not _in_window(occurred_at, since, until):
            continue
        inbound_id = str(row["email_id"])
        processing = {
            "inbound_email_id": inbound_id,
            "status": row["status"],
            "reply_email_id": row["reply_email_id"],
            "error": row["error"],
        }
        index = archive_by_inbound.get(inbound_id)
        if index is not None:
            messages[index]["metadata"]["processing"] = processing
            continue
        messages.append(
            _message(
                evidence_id=f"email-gap:{inbound_id}",
                kind="email_processing_gap",
                surface="direct_email",
                direction="member_to_oliver",
                context_id=str(row["thread_id"] or inbound_id),
                occurred_at=occurred_at,
                body=None,
                subject=row["subject"],
                visibility="member_private",
                links={"inbound_email_id": inbound_id},
                metadata={
                    "processing": processing,
                    "sender_email": row["from_email"],
                    "evidence_gap": "inbound message was not found in mail_messages",
                },
            )
        )
    return messages


def _proactive_discord(
    conn: sqlite3.Connection, since: datetime, until: datetime
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT id, idempotency_key, payload_json, status, attempts, provider_ref_json, "
        "last_error, created_at, delivered_at FROM outbox_messages "
        "WHERE kind = 'discord' ORDER BY created_at, id"
    ):
        occurred_at = row["delivered_at"] or row["created_at"]
        if not _in_window(occurred_at, since, until):
            continue
        payload = _json(row["payload_json"], {})
        messages.append(
            _message(
                evidence_id=f"discord-outbox:{row['id']}",
                kind="discord_proactive",
                surface="discord",
                direction="oliver_to_club",
                context_id=str(payload.get("channel_id") or row["idempotency_key"]),
                occurred_at=occurred_at,
                body=payload.get("content"),
                delivery_status=row["status"],
                visibility="club_shared",
                links={"outbox_id": row["id"]},
                metadata={
                    "idempotency_key": row["idempotency_key"],
                    "attempts": row["attempts"],
                    "last_error": row["last_error"],
                    "provider": _json(row["provider_ref_json"], {}),
                },
            )
        )
    return messages


def _workflow_evidence(
    conn: sqlite3.Connection, since: datetime, until: datetime
) -> list[dict[str, Any]]:
    """Collect state needed to judge cooldown, expiry, dedupe, and processing decisions."""
    context: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT id, member_id, book_slug, thread_id, state, draft_json, rounds, created_at, "
        "updated_at FROM review_drafts ORDER BY updated_at, id"
    ):
        if row["state"] not in {"awaiting_reply", "awaiting_confirm", "parked"} and not _in_window(
            row["updated_at"], since, until
        ):
            continue
        context.append(
            {
                "context_type": "review_draft",
                "context_id": f"review-draft:{row['id']}",
                "occurred_at": row["updated_at"],
                "visibility": "member_private",
                "data": dict(row),
            }
        )
    for row in conn.execute(
        "SELECT id, member_id, meeting_id, actor, category, kind, detail, surface, source, "
        "occurred_at, created_at FROM events ORDER BY occurred_at, id"
    ):
        if not _in_window(row["occurred_at"], since, until):
            continue
        context.append(
            {
                "context_type": "event",
                "context_id": f"event:{row['id']}",
                "occurred_at": row["occurred_at"],
                "visibility": "member_private" if row["member_id"] is not None else "club_shared",
                "data": dict(row),
            }
        )
    for row in conn.execute(
        "SELECT id, kind, title, body, status, attempts, last_error, next_attempt_at, created_at, "
        "posted_at FROM activity_events ORDER BY created_at, id"
    ):
        occurred_at = row["posted_at"] or row["created_at"]
        if not _in_window(occurred_at, since, until):
            continue
        context.append(
            {
                "context_type": "activity_event",
                "context_id": f"activity-event:{row['id']}",
                "occurred_at": occurred_at,
                "visibility": "club_shared",
                "data": dict(row),
            }
        )
    job_rows: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute(
        "SELECT id, job_name, lease_owner, started_at, finished_at, outcome, duration_ms, "
        "processed_count, error FROM job_runs ORDER BY started_at, id"
    ):
        if not _in_window(row["started_at"], since, until):
            continue
        job_rows.setdefault(str(row["job_name"]), []).append(row)
    for job_name, rows in sorted(job_rows.items()):
        latest = rows[-1]
        context.append(
            {
                "context_type": "job_run_summary",
                "context_id": f"job-run-summary:{job_name}",
                "occurred_at": latest["started_at"],
                "visibility": "internal",
                "data": {
                    "job_name": job_name,
                    "window_count": len(rows),
                    "outcomes": dict(sorted(Counter(str(row["outcome"]) for row in rows).items())),
                    "latest": dict(latest),
                },
            }
        )
        for row in rows:
            if row["outcome"] in {"success", "succeeded"}:
                continue
            context.append(
                {
                    "context_type": "job_run_failure",
                    "context_id": f"job-run:{row['id']}",
                    "occurred_at": row["started_at"],
                    "visibility": "internal",
                    "data": dict(row),
                }
            )
    context.sort(
        key=lambda item: (
            _parse_time(item["occurred_at"]) or datetime.min.replace(tzinfo=timezone.utc),
            item["context_id"],
        )
    )
    return context


def _summary(
    messages: list[dict[str, Any]], context_count: int, workflow_count: int
) -> dict[str, Any]:
    by_surface_direction = Counter(f"{item['surface']}:{item['direction']}" for item in messages)
    delivery = Counter(
        str(item["delivery_status"]) for item in messages if item.get("delivery_status") is not None
    )
    gaps = sum(1 for item in messages if item["kind"] == "email_processing_gap")
    return {
        "message_count": len(messages),
        "discord_context_count": context_count,
        "workflow_context_count": workflow_count,
        "by_surface_direction": dict(sorted(by_surface_direction.items())),
        "delivery_status": dict(sorted(delivery.items())),
        "evidence_gaps": gaps,
    }


def collect_evidence(
    db_path: str | Path | None = None,
    *,
    since: datetime,
    until: datetime,
    oliver_email: str | None = None,
    mailing_list: str | None = None,
) -> dict[str, Any]:
    """Collect one consistent read-only evidence window from Oliver SQLite."""
    # Opening the connection loads the repo's .env without overriding the caller.
    conn = connect_read_only(db_path)
    oliver_email = oliver_email or os.environ.get("OLIVER_EMAIL_ADDRESS", "oliver@rwbookclub.com")
    mailing_list = mailing_list or os.environ.get(
        "BOOK_CLUB_MAILING_LIST_ADDRESS", "rwbookclub@googlegroups.com"
    )
    try:
        _assert_contract(conn)
        conn.execute("BEGIN")
        discord_messages, interactions, context = _discord_evidence(conn, since, until)
        messages = discord_messages
        messages.extend(
            _email_evidence(
                conn,
                since,
                until,
                oliver_email=oliver_email,
                mailing_list=mailing_list,
            )
        )
        messages.extend(_proactive_discord(conn, since, until))
        workflow_context = _workflow_evidence(conn, since, until)
        messages.sort(
            key=lambda item: (
                _parse_time(item.get("occurred_at")) or datetime.min.replace(tzinfo=timezone.utc),
                item["evidence_id"],
            )
        )
        return {
            "schema_version": 1,
            "generated_at": _iso(datetime.now(timezone.utc)),
            "window": {"since": _iso(since), "until": _iso(until)},
            "summary": _summary(messages, len(context), len(workflow_context)),
            "messages": messages,
            "discord_interactions": interactions,
            "discord_context": context,
            "workflow_context": workflow_context,
        }
    finally:
        conn.rollback()
        conn.close()


def write_raw_report(
    report: dict[str, Any],
    path: str | Path,
    *,
    raw_root: str | Path = RAW_RESULTS_ROOT,
) -> Path:
    """Write raw evidence only inside the designated gitignored evaluator root."""
    destination = Path(path).expanduser().resolve()
    allowed = Path(raw_root).expanduser().resolve()
    if not destination.is_relative_to(allowed):
        raise EvidenceError(f"raw evaluator evidence may only be written below {allowed}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return destination


def _default_destination(now: datetime) -> Path:
    run_id = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return RAW_RESULTS_ROOT / run_id / "evidence.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="override OLIVER_DB_PATH")
    parser.add_argument("--days", type=int, default=7, help="UTC lookback window (default: 7)")
    parser.add_argument("--since", help="explicit inclusive ISO-8601 start")
    parser.add_argument("--until", help="explicit inclusive ISO-8601 end (default: now)")
    parser.add_argument("--output", help="raw output path below AGENT-TEAM/notes/evaluator")
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="collect and print coverage only; discard raw evidence",
    )
    args = parser.parse_args()
    now = _parse_time(args.until) if args.until else datetime.now(timezone.utc)
    if now is None:
        raise SystemExit("--until must be a valid ISO-8601 timestamp")
    since = _parse_time(args.since) if args.since else now - timedelta(days=max(1, args.days))
    if since is None:
        raise SystemExit("--since must be a valid ISO-8601 timestamp")
    if since > now:
        raise SystemExit("--since must not be after --until")
    report = collect_evidence(args.database, since=since, until=now)
    destination = None
    if not args.no_write:
        destination = write_raw_report(report, args.output or _default_destination(now))
    print(
        json.dumps(
            {
                "output": str(destination.relative_to(ROOT)) if destination else None,
                "window": report["window"],
                **report["summary"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
