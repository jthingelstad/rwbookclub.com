"""tools.dispatch error paths + a couple of happy paths."""

from __future__ import annotations

import json


class TestDispatchErrors:
    def test_unknown_tool(self):
        from agent.tools import dispatch
        result = json.loads(dispatch("nonexistent_tool", {}, {}))
        assert result == {"error": "unknown tool nonexistent_tool"}

    def test_get_book_unknown(self):
        from agent.tools import dispatch
        result = json.loads(dispatch("get_book", {"book": "no-such-book"}, {}))
        assert result == {"error": "no such book"}

    def test_get_author_unknown(self):
        from agent.tools import dispatch
        result = json.loads(dispatch("get_author", {"author": "Nobody Particular"}, {}))
        assert result == {"error": "no such author"}

    def test_missing_required_arg_returns_error(self):
        """T1.1: tool exceptions are caught and returned as error dicts (also logged)."""
        from agent.tools import dispatch
        # get_book requires {"book": ...}; missing key → KeyError, caught, returned.
        result = json.loads(dispatch("get_book", {}, {}))
        assert "error" in result
        assert "KeyError" in result["error"]


class TestDispatchHappyPaths:
    def test_find_books_returns_list(self):
        from agent.tools import dispatch
        result = json.loads(dispatch("find_books", {"query": "technology"}, {}))
        assert isinstance(result, list)

    def test_club_stats_returns_dict(self):
        from agent.tools import dispatch
        result = json.loads(dispatch("club_stats", {}, {}))
        assert isinstance(result, dict)
        assert "totalRead" in result

    def test_related_books_returns_matches(self):
        from agent.tools import dispatch

        result = json.loads(dispatch("related_books", {"book": "the-martian"}, {}))
        assert result["book"]["slug"] == "the-martian"
        assert result["related"]

    def test_compare_books_returns_side_by_side(self):
        from agent.tools import dispatch

        result = json.loads(dispatch(
            "compare_books", {"books": ["the-martian", "thinking-in-systems"]}, {}
        ))
        assert [b["slug"] for b in result["books"]] == ["the-martian", "thinking-in-systems"]

    def test_review_summary_returns_aggregate(self):
        from agent.tools import dispatch

        result = json.loads(dispatch("review_summary", {"book": "the-martian"}, {}))
        assert result["book"]["slug"] == "the-martian"
        assert result["reviewCount"] >= 1

    def test_upcoming_meetings_returns_list(self):
        from agent.tools import dispatch
        result = json.loads(dispatch("upcoming_meetings", {}, {}))
        assert isinstance(result, list)

    def test_remember_records_source_metadata(self, fresh_db):
        from agent.tools import dispatch

        result = json.loads(dispatch(
            "remember",
            {"note": "likes infrastructure books", "scope": "member", "subject": "nick"},
            {"speaker": "Nick", "speaker_user_id": "u1", "source_message_id": "m1"},
        ))
        assert result["saved"] is True
        memories = fresh_db.get_memories(subject="nick")
        assert memories[0]["source"] == "Nick"
        assert memories[0]["source_user_id"] == "u1"
        assert memories[0]["source_message_id"] == "m1"

    def test_identity_status_uses_linked_speaker(self, fresh_db):
        from agent.tools import dispatch

        fresh_db.link_member_identity("u1", "jamie")
        result = json.loads(dispatch("identity_status", {}, {
            "speaker_user_id": "u1",
            "member_slug": "jamie",
        }))
        assert result["speakerMemberSlug"] == "jamie"
        assert result["speakerMember"]["name"] == "Jamie"

    def test_current_meeting_status_returns_rules(self, fresh_db):
        from agent.tools import dispatch

        result = json.loads(dispatch("current_meeting_status", {}, {}))
        assert result["rules"]["standingDate"] == "last Tuesday of the month"
        assert result["counts"]["quorumRequired"] == 3

    def test_record_availability_requires_linked_speaker(self, fresh_db):
        from agent.tools import dispatch

        result = json.loads(dispatch("record_availability", {"status": "yes"}, {}))
        assert "error" in result

    def test_record_availability_saves_for_speaker(self, fresh_db):
        from agent.tools import dispatch

        fresh_db.link_member_identity("u1", "jamie")
        result = json.loads(dispatch("record_availability", {"status": "yes"}, {
            "speaker_user_id": "u1",
            "member_slug": "jamie",
            "channel_id": "ch1",
        }))
        assert result["saved"] is True
        rows = fresh_db.attendance_for_meeting(result["meetingStatus"]["meeting"]["meetingKey"])
        assert rows[0]["member_slug"] == "jamie"
        assert rows[0]["status"] == "yes"

    def test_recent_channel_context_returns_logged_messages(self, fresh_db):
        from agent.tools import dispatch

        fresh_db.log_message("ch1", "user", "hello", speaker="Jamie")
        result = json.loads(dispatch("recent_channel_context", {"limit": 5}, {"channel_id": "ch1"}))
        assert result[0]["content"] == "hello"
