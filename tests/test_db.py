"""SQLite helpers — memories, responses+feedback (T2 reactions), reminders (T1.3)."""

from __future__ import annotations

import pytest


class TestMemories:
    def test_add_and_get(self, fresh_db):
        db = fresh_db
        mid = db.add_memory("loves dense history", scope="member", subject="loren")
        assert mid > 0
        out = db.get_memories(subject="loren")
        assert len(out) == 1
        assert out[0]["note"] == "loves dense history"
        assert out[0]["scope"] == "member"

    def test_scope_filter(self, fresh_db):
        db = fresh_db
        db.add_memory("club fact", scope="club")
        db.add_memory("member fact", scope="member", subject="tom")
        club = db.get_memories(scope="club")
        assert len(club) == 1
        assert club[0]["note"] == "club fact"

    def test_query_filter(self, fresh_db):
        db = fresh_db
        db.add_memory("loves the sea", scope="member", subject="x")
        db.add_memory("hates the cold", scope="member", subject="x")
        sea = db.get_memories(query="sea")
        assert len(sea) == 1
        assert "sea" in sea[0]["note"]

    def test_update_and_delete(self, fresh_db):
        db = fresh_db
        mid = db.add_memory("old note", source_user_id="u1", source_message_id="m1")
        assert db.update_memory(mid, "new note")
        out = db.get_memories()
        assert out[0]["note"] == "new note"
        assert out[0]["source_user_id"] == "u1"
        assert out[0]["source_message_id"] == "m1"
        assert db.delete_memory(mid)
        assert db.get_memories() == []


class TestMemberIdentities:
    def test_link_and_lookup(self, fresh_db):
        db = fresh_db
        db.link_member_identity("123", "jamie", linked_by="admin")
        assert db.member_slug_for_user("123") == "jamie"
        assert db.member_slug_for_user("999") is None
        row = db.identity_for_member("jamie")
        assert row["discord_user_id"] == "123"
        assert row["linked_by"] == "admin"

    def test_relink_updates(self, fresh_db):
        db = fresh_db
        db.link_member_identity("123", "jamie")
        db.link_member_identity("123", "tom")
        assert db.member_slug_for_user("123") == "tom"
        rows = db.list_member_identities()
        assert len(rows) == 1
        assert rows[0]["member_slug"] == "tom"

    def test_email_link_and_lookup(self, fresh_db):
        db = fresh_db
        db.link_member_email("Jamie@Thingelstad.COM", "jamie", linked_by="admin")
        assert db.member_slug_for_email("jamie@thingelstad.com") == "jamie"
        assert db.member_slug_for_email("JAMIE@THINGELSTAD.COM") == "jamie"
        row = db.email_for_member("jamie")
        assert row["email"] == "jamie@thingelstad.com"
        assert row["linked_by"] == "admin"


class TestFeedback:
    def test_response_log_and_lookup(self, fresh_db):
        db = fresh_db
        db.log_response(message_id="abc", channel_id="ch1",
                        speaker="Jamie", question="hi", reply="hello")
        assert db.is_oliver_message("abc")
        assert not db.is_oliver_message("xyz")

    def test_feedback_round_trip(self, fresh_db):
        db = fresh_db
        db.log_response(message_id="msg1", channel_id="ch1",
                        speaker="Jamie", question="q?", reply="a.")
        db.add_feedback(message_id="msg1", channel_id="ch1",
                        user_id="u1", user_name="Jamie", reaction="up")
        db.add_feedback(message_id="msg1", channel_id="ch1",
                        user_id="u2", user_name="Tom", reaction="down")
        stats = db.feedback_stats()
        assert stats["up"] == 1
        assert stats["down"] == 1
        assert stats["total"] == 2
        # The recent_down join surfaces the original question:
        assert stats["recent_down"][0]["question"] == "q?"
        assert stats["recent_down"][0]["user_name"] == "Tom"


class TestReminders:
    def test_due_then_marked(self, fresh_db):
        """T1.3 regression: reminders are queryable and can be marked fired."""
        db = fresh_db
        rid = db.add_reminder("2020-01-01T00:00:00+00:00", "old reminder",
                              channel_id="ch1", created_by="Jamie")
        due = db.due_reminders()
        assert len(due) == 1
        assert due[0]["id"] == rid
        db.mark_reminder_fired(rid)
        assert db.due_reminders() == []

    def test_future_reminders_not_due(self, fresh_db):
        db = fresh_db
        db.add_reminder("2099-12-31T00:00:00+00:00", "future reminder")
        assert db.due_reminders() == []


class TestNotificationsDedup:
    def test_sent_keys_round_trip(self, fresh_db):
        db = fresh_db
        assert db.sent_keys() == set()
        db.mark_sent("test-key-1")
        db.mark_sent("test-key-2")
        assert db.sent_keys() == {"test-key-1", "test-key-2"}
        # Idempotent — re-marking is safe.
        db.mark_sent("test-key-1")
        assert db.sent_keys() == {"test-key-1", "test-key-2"}


class TestRollCall:
    def test_roll_call_and_attendance_round_trip(self, fresh_db):
        db = fresh_db
        db.upsert_roll_call(
            meeting_key="book-a", channel_id="ch1", message_id="msg1", opened_by="admin"
        )
        row = db.get_roll_call("book-a")
        assert row["status"] == "open"
        assert row["message_id"] == "msg1"

        db.set_attendance(
            meeting_key="book-a", member_slug="jamie", status="yes",
            updated_by_user_id="u1", source="button",
        )
        db.set_attendance(
            meeting_key="book-a", member_slug="tom", status="no",
            updated_by_user_id="u2", source="chat",
        )
        attendance = db.attendance_for_meeting("book-a")
        assert {r["member_slug"]: r["status"] for r in attendance} == {
            "jamie": "yes",
            "tom": "no",
        }
        assert db.close_roll_call("book-a")
        assert db.get_roll_call("book-a")["status"] == "closed"


class TestSearchConversations:
    def test_spans_channels_newest_first(self, fresh_db):
        db = fresh_db
        db.log_message("ch1", "user", "we discussed dune at length", speaker="Jamie")
        db.log_message("ch2", "user", "dune came up in book-talk too", speaker="Tom")
        rows = db.search_conversations("dune")
        assert {r["channel_id"] for r in rows} == {"ch1", "ch2"}
        # Newest first: the ch2 row was inserted last, so it leads.
        assert rows[0]["channel_id"] == "ch2"

    def test_multi_term_and_match(self, fresh_db):
        db = fresh_db
        db.log_message("ch1", "user", "the mars trilogy is great", speaker="Jamie")
        db.log_message("ch1", "user", "mars on its own, no series", speaker="Tom")
        # Both terms must appear (anywhere); only the first row has "trilogy".
        rows = db.search_conversations("mars trilogy")
        assert len(rows) == 1
        assert "trilogy" in rows[0]["content"]

    def test_limit_respected(self, fresh_db):
        db = fresh_db
        for i in range(5):
            db.log_message("ch1", "user", f"note {i} about kafka", speaker="Jamie")
        rows = db.search_conversations("kafka", limit=2)
        assert len(rows) == 2

    def test_empty_query_returns_empty(self, fresh_db):
        db = fresh_db
        db.log_message("ch1", "user", "something", speaker="Jamie")
        assert db.search_conversations("") == []
        assert db.search_conversations("   ") == []

    def test_channel_ids_filter(self, fresh_db):
        db = fresh_db
        db.log_message("ch1", "user", "orwell in general", speaker="Jamie")
        db.log_message("ch2", "user", "orwell in book-talk", speaker="Tom")
        rows = db.search_conversations("orwell", channel_ids=["ch1"])
        assert len(rows) == 1
        assert rows[0]["channel_id"] == "ch1"


class TestProposals:
    def test_proposal_lifecycle(self, fresh_db):
        db = fresh_db
        pid = db.add_proposal(
            kind="meeting_notice",
            title="Post quorum warning",
            body="Only two members are confirmed.",
            channel_id="ch1",
            source_user_id="u1",
        )
        rows = db.list_proposals()
        assert rows[0]["id"] == pid
        assert rows[0]["status"] == "pending"
        assert db.resolve_proposal(pid, "accepted", resolved_by="admin")
        assert db.list_proposals() == []


class TestInboundEmail:
    def test_processing_claim_dedupes(self, fresh_db):
        db = fresh_db
        assert db.mark_email_processing(email_id="m1", thread_id="t1", from_email="a@example.test")
        assert not db.mark_email_processing(email_id="m1")
        assert not db.email_processed("m1")
        db.mark_email_processed("m1", reply_email_id="reply1")
        assert db.email_processed("m1")

    def test_ignored_counts_as_processed(self, fresh_db):
        db = fresh_db
        assert db.mark_email_processing(email_id="m2")
        db.mark_email_processed("m2", status="ignored")
        assert db.email_processed("m2")

    def test_failed_can_be_claimed_again_for_retry(self, fresh_db):
        db = fresh_db
        assert db.mark_email_processing(email_id="m3")
        db.mark_email_processed("m3", status="failed", error="boom")
        assert not db.email_processed("m3")
        assert db.mark_email_processing(email_id="m3")


class TestReadingStatus:
    def test_set_and_get_reading_status(self, fresh_db):
        db = fresh_db
        db.set_reading_status(
            meeting_key="a-world-appears",
            member_slug="jamie",
            status="on_track",
            progress="halfway",
            page=120,
            percent=50,
            source="email",
            updated_by="email:jamie@thingelstad.com",
        )
        row = db.reading_status_for_member("a-world-appears", "jamie")
        assert row["status"] == "on_track"
        assert row["progress"] == "halfway"
        rows = db.reading_status_for_meeting("a-world-appears")
        assert rows[0]["member_slug"] == "jamie"

    def test_reading_status_validates_values(self, fresh_db):
        db = fresh_db
        import pytest

        with pytest.raises(ValueError):
            db.set_reading_status(meeting_key="m", member_slug="jamie", status="unknown")
        with pytest.raises(ValueError):
            db.set_reading_status(meeting_key="m", member_slug="jamie", status="started", percent=101)


class TestActivityEvents:
    def test_activity_queue_round_trip(self, fresh_db):
        db = fresh_db
        aid = db.add_activity("email_sent", "Email sent", "To: jamie@example.test")
        rows = db.pending_activity()
        assert rows[0]["id"] == aid
        assert rows[0]["kind"] == "email_sent"
        assert rows[0]["title"] == "Email sent"
        db.mark_activity_posted(aid)
        assert db.pending_activity() == []

    def test_activity_failure_retries_then_dead_letters(self, fresh_db):
        db = fresh_db
        aid = db.add_activity("email_sent", "Email sent", "To: jamie@example.test")
        db.mark_activity_failed(aid, "webhook 500", max_attempts=2, retry_delay_seconds=0)
        assert db.pending_activity()[0]["attempts"] == 1
        db.mark_activity_failed(aid, "webhook 500", max_attempts=2, retry_delay_seconds=0)
        assert db.pending_activity() == []


class TestInboundEmails:
    def test_stale_processing_email_can_be_reclaimed(self, fresh_db):
        db = fresh_db
        assert db.mark_email_processing(email_id="m1", from_email="jamie@example.test")
        assert not db.mark_email_processing(email_id="m1", from_email="jamie@example.test")
        with db.connect() as conn:
            conn.execute(
                "UPDATE inbound_emails SET processed_at = '2000-01-01 00:00:00' WHERE email_id = 'm1'"
            )
        assert db.mark_email_processing(email_id="m1", from_email="jamie@example.test")


class TestMemberContacts:
    def test_contact_and_email_open_round_trip(self, fresh_db):
        db = fresh_db
        cid = db.add_member_contact(
            meeting_key="a-world-appears",
            member_slug="jamie",
            kind="reading_checkin",
            surface="email",
            direction="outbound",
            status="sent",
            subject="Reading check-in",
        )
        db.add_email_tracking(
            token="tok1",
            contact_id=cid,
            meeting_key="a-world-appears",
            member_slug="jamie",
            kind="reading_checkin",
            subject="Reading check-in",
        )
        row = db.record_email_open("tok1", remote_addr="127.0.0.1", user_agent="test")
        assert row["member_slug"] == "jamie"
        contacts = db.member_contacts_for_meeting("a-world-appears")
        assert contacts[0]["status"] == "opened"
        summary = db.email_open_summary("a-world-appears")
        assert summary["jamie"]["open_count"] == 1
