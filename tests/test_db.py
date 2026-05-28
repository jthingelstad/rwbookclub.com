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
