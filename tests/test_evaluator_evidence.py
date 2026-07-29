from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.evaluator_evidence import EvidenceError, collect_evidence, write_raw_report
from scripts.read_only_db import connect_read_only

SINCE = datetime(2026, 7, 20, tzinfo=timezone.utc)
UNTIL = datetime(2026, 7, 30, tzinfo=timezone.utc)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "production-shaped.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE responses (
            message_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, speaker TEXT,
            question TEXT, reply TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE feedback (
            id INTEGER PRIMARY KEY, message_id TEXT NOT NULL, channel_id TEXT NOT NULL,
            user_id TEXT NOT NULL, user_name TEXT, reaction TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE conversations (
            id INTEGER PRIMARY KEY, channel_id TEXT NOT NULL, role TEXT NOT NULL,
            speaker TEXT, content TEXT NOT NULL, created_at TEXT NOT NULL, member_slug TEXT
        );
        CREATE TABLE mail_messages (
            message_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, parent_message_id TEXT,
            source TEXT NOT NULL, source_ref TEXT, list_id TEXT, from_email TEXT,
            from_name TEXT, member_id INTEGER, to_json TEXT, cc_json TEXT, subject TEXT,
            sent_at TEXT, received_at TEXT, body_text TEXT, body_clean TEXT,
            imported_at TEXT NOT NULL, processed_inbound_email_id TEXT
        );
        CREATE TABLE inbound_emails (
            email_id TEXT PRIMARY KEY, thread_id TEXT, from_email TEXT, subject TEXT,
            status TEXT NOT NULL, reply_email_id TEXT, error TEXT, received_at TEXT,
            processed_at TEXT NOT NULL
        );
        CREATE TABLE outbox_messages (
            id INTEGER PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
            payload_json TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL,
            provider_ref_json TEXT, last_error TEXT, created_at TEXT NOT NULL,
            delivered_at TEXT
        );
        CREATE TABLE review_drafts (
            id INTEGER PRIMARY KEY, member_id INTEGER NOT NULL, book_slug TEXT NOT NULL,
            thread_id TEXT, state TEXT NOT NULL, draft_json TEXT, rounds INTEGER NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY, member_id INTEGER, meeting_id INTEGER, actor TEXT NOT NULL,
            category TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT, surface TEXT, source TEXT,
            occurred_at TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE activity_events (
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT,
            status TEXT NOT NULL, created_at TEXT NOT NULL, posted_at TEXT,
            attempts INTEGER NOT NULL, last_error TEXT, next_attempt_at TEXT
        );
        CREATE TABLE job_runs (
            id INTEGER PRIMARY KEY, job_name TEXT NOT NULL, lease_owner TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT, outcome TEXT NOT NULL,
            duration_ms INTEGER, processed_count INTEGER NOT NULL, error TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO responses VALUES (?, ?, ?, ?, ?, ?)",
        (
            "discord-reply-1",
            "ask-oliver",
            "Member A",
            "When is the next meeting?",
            "Next Tuesday at 6:30.",
            "2026-07-21T15:00:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO feedback VALUES (?, ?, ?, ?, ?, ?, ?)",
        (1, "discord-reply-1", "ask-oliver", "member-a", "Member A", "up", "2026-07-21T15:01:00Z"),
    )
    conn.executemany(
        "INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                1,
                "ask-oliver",
                "user",
                "Member A",
                "When is the next meeting?",
                "2026-07-21T15:00:00Z",
                "member-a",
            ),
            (
                2,
                "ask-oliver",
                "assistant",
                None,
                "Next Tuesday at 6:30.",
                "2026-07-21T15:00:01Z",
                None,
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO mail_messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "mail-in-1",
                "thread-direct",
                None,
                "live_jmap_inbound",
                "provider-inbound",
                None,
                "member@example.invalid",
                "Member A",
                1,
                json.dumps(["oliver@rwbookclub.com"]),
                "[]",
                "A private question",
                None,
                "2026-07-22T10:00:00Z",
                "Exact private inbound body",
                "Exact private inbound body",
                "2026-07-22T10:00:01Z",
                "provider-inbound",
            ),
            (
                "jmap-sent:provider-reply",
                "thread-direct",
                "mail-in-1",
                "live_jmap_outbound",
                "provider-reply",
                None,
                "oliver@rwbookclub.com",
                "Oliver",
                1,
                json.dumps(["member@example.invalid"]),
                "[]",
                "Re: A private question",
                "2026-07-22T10:05:00Z",
                None,
                "Archived reply body",
                "Archived reply body",
                "2026-07-22T10:05:01Z",
                None,
            ),
        ],
    )
    conn.execute(
        "INSERT INTO inbound_emails VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "provider-inbound",
            "thread-direct",
            "member@example.invalid",
            "A private question",
            "processed",
            "provider-reply",
            None,
            "2026-07-22T10:00:00Z",
            "2026-07-22T10:05:02Z",
        ),
    )
    outbox = [
        (
            1,
            "email:reply:provider-inbound",
            "email",
            json.dumps(
                {
                    "to": ["member@example.invalid"],
                    "subject": "Re: A private question",
                    "body": "Final signed reply body",
                    "policy": "linked_member",
                }
            ),
            "delivered",
            1,
            json.dumps({"emailId": "provider-reply", "threadId": "thread-direct"}),
            None,
            "2026-07-22T10:04:00Z",
            "2026-07-22T10:05:00Z",
        ),
        (
            2,
            "email:review-ask:2026-W30:member-a",
            "email",
            json.dumps(
                {
                    "to": ["member@example.invalid"],
                    "subject": "Your review?",
                    "body": "Exact proactive review ask",
                    "policy": "linked_member",
                }
            ),
            "delivered",
            1,
            json.dumps({"emailId": "provider-proactive"}),
            None,
            "2026-07-23T15:00:00Z",
            "2026-07-23T15:00:01Z",
        ),
        (
            3,
            "discord:meeting-reminder:1",
            "discord",
            json.dumps({"channel_id": "book-talk", "content": "Exact proactive Discord post"}),
            "delivered",
            1,
            json.dumps({"messageId": "discord-proactive-1"}),
            None,
            "2026-07-24T15:00:00Z",
            "2026-07-24T15:00:01Z",
        ),
    ]
    conn.executemany("INSERT INTO outbox_messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", outbox)
    conn.execute(
        "INSERT INTO review_drafts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            1,
            1,
            "synthetic-book",
            "thread-review",
            "awaiting_reply",
            json.dumps({"body": ""}),
            0,
            "2026-07-23T14:59:00Z",
            "2026-07-23T15:00:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            1,
            1,
            None,
            "oliver",
            "reading",
            "review_requested",
            json.dumps({"book_slug": "synthetic-book", "thread_id": "thread-review"}),
            "email",
            None,
            "2026-07-23T15:00:00Z",
            "2026-07-23T15:00:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO activity_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            1,
            "review_drive",
            "Review requested",
            "Synthetic review request",
            "posted",
            "2026-07-23T15:00:00Z",
            "2026-07-23T15:00:02Z",
            1,
            None,
            None,
        ),
    )
    conn.execute(
        "INSERT INTO job_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            1,
            "review_drive",
            "fixture",
            "2026-07-23T14:59:58Z",
            "2026-07-23T15:00:03Z",
            "success",
            5000,
            1,
            None,
        ),
    )
    conn.commit()
    conn.close()
    return path


def test_read_only_connection_rejects_writes(tmp_path: Path) -> None:
    path = _database(tmp_path)
    conn = connect_read_only(path)
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError, match="readonly|read-only"):
        conn.execute("DELETE FROM responses")
    conn.close()


def test_collector_merges_all_surfaces_without_mutating_database(tmp_path: Path) -> None:
    path = _database(tmp_path)
    before_hash = _hash(path)
    before_sidecars = sorted(item.name for item in tmp_path.iterdir())
    with sqlite3.connect(path) as conn:
        before_schema = conn.execute("PRAGMA schema_version").fetchone()[0]
        before_rows = conn.execute("SELECT COUNT(*) FROM outbox_messages").fetchone()[0]

    report = collect_evidence(
        path,
        since=SINCE,
        until=UNTIL,
        oliver_email="oliver@rwbookclub.com",
        mailing_list="rwbookclub@googlegroups.com",
    )

    assert report["summary"] == {
        "message_count": 6,
        "discord_context_count": 2,
        "workflow_context_count": 4,
        "by_surface_direction": {
            "direct_email:member_to_oliver": 1,
            "direct_email:oliver_to_member": 2,
            "discord:member_to_oliver": 1,
            "discord:oliver_to_club": 2,
        },
        "delivery_status": {"delivered": 3},
        "evidence_gaps": 0,
    }
    merged = next(
        item
        for item in report["messages"]
        if item["links"].get("mail_message_id") == "jmap-sent:provider-reply"
    )
    assert merged["body"] == "Final signed reply body"
    assert merged["delivery_status"] == "delivered"
    assert merged["links"]["outbox_id"] == 1
    assert (
        len(
            [
                item
                for item in report["messages"]
                if item["metadata"].get("outbox", {}).get("outbox_id") == 1
            ]
        )
        == 1
    )
    proactive = next(item for item in report["messages"] if item["links"].get("outbox_id") == 2)
    assert proactive["body"] == "Exact proactive review ask"
    inbound = next(
        item for item in report["messages"] if item["links"].get("mail_message_id") == "mail-in-1"
    )
    assert inbound["metadata"]["processing"]["status"] == "processed"
    assert {item["context_type"] for item in report["workflow_context"]} == {
        "activity_event",
        "event",
        "job_run_summary",
        "review_draft",
    }

    assert _hash(path) == before_hash
    assert sorted(item.name for item in tmp_path.iterdir()) == before_sidecars
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA schema_version").fetchone()[0] == before_schema
        assert conn.execute("SELECT COUNT(*) FROM outbox_messages").fetchone()[0] == before_rows


def test_missing_inbound_archive_is_an_explicit_gap(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO inbound_emails VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "missing-provider-id",
                "missing-thread",
                "member@example.invalid",
                "Missing archive",
                "failed",
                None,
                "archive failed",
                "2026-07-25T10:00:00Z",
                "2026-07-25T10:00:01Z",
            ),
        )

    report = collect_evidence(path, since=SINCE, until=UNTIL)
    gap = next(item for item in report["messages"] if item["kind"] == "email_processing_gap")
    assert gap["body"] is None
    assert gap["metadata"]["processing"]["status"] == "failed"
    assert report["summary"]["evidence_gaps"] == 1


def test_raw_private_evidence_can_only_be_written_below_local_root(tmp_path: Path) -> None:
    allowed = tmp_path / "notes" / "evaluator"
    report = {"messages": [{"body": "private"}]}
    destination = write_raw_report(report, allowed / "run" / "evidence.json", raw_root=allowed)
    assert json.loads(destination.read_text()) == report
    with pytest.raises(EvidenceError, match="may only be written"):
        write_raw_report(report, tmp_path / "committed.json", raw_root=allowed)


def test_evaluator_scripts_have_no_production_delivery_imports() -> None:
    root = Path(__file__).resolve().parent.parent
    collector = (root / "scripts" / "evaluator_evidence.py").read_text()
    synthetic = (root / "scripts" / "eval_oliver.py").read_text()
    assert "from agent" not in collector
    assert "import agent" not in collector
    assert "from agent.mail import outbound" not in synthetic
    assert "from agent.mail.email_jmap" not in synthetic
    assert "agent.publish" not in synthetic
    assert "synthetic evaluator attempted to use a production delivery path" in synthetic
    assert "_assert_scratch_outbox_empty()" in synthetic
    assert synthetic.index('os.environ["FASTMAIL_JMAP_TOKEN"] = ""') < synthetic.index(
        "from agent import"
    )
