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

    def test_propose_action_stages_admin_review(self, fresh_db):
        from agent.tools import dispatch

        result = json.loads(dispatch("propose_action", {
            "kind": "meeting_notice",
            "title": "Warn about quorum",
            "body": "Only two members are confirmed.",
        }, {"channel_id": "ch1", "speaker_user_id": "u1"}))
        assert result["saved"] is True
        proposals = fresh_db.list_proposals()
        assert proposals[0]["title"] == "Warn about quorum"
        assert proposals[0]["channel_id"] == "ch1"

    def test_open_proposals_returns_pending(self, fresh_db):
        from agent.tools import dispatch

        fresh_db.add_proposal(kind="other", title="Check this", body="A note")
        result = json.loads(dispatch("open_proposals", {}, {}))
        assert result[0]["title"] == "Check this"

    def test_search_discussion_returns_rows_with_channel_label(self, fresh_db):
        from agent.tools import dispatch

        fresh_db.log_message("100", "user", "we loved the foundation series", speaker="Jamie")
        fresh_db.log_message("200", "user", "foundation came up in book-talk", speaker="Tom")
        result = json.loads(dispatch("search_discussion", {"query": "foundation"}, {}))
        assert isinstance(result, list)
        assert len(result) == 2
        # Every row carries a human "channel" label (falls back to the raw id here).
        assert all("channel" in r for r in result)

    def test_search_discussion_truncates_content(self, fresh_db):
        from agent.tools import dispatch

        fresh_db.log_message("100", "user", "kafka " * 200, speaker="Jamie")
        result = json.loads(dispatch("search_discussion", {"query": "kafka"}, {}))
        assert len(result[0]["content"]) == 300

    def test_search_discussion_clamps_limit(self, fresh_db):
        from agent.tools import dispatch

        for i in range(30):
            fresh_db.log_message("100", "user", f"note {i} mentions borges", speaker="Jamie")
        result = json.loads(dispatch("search_discussion", {"query": "borges", "limit": 99}, {}))
        assert len(result) == 20  # clamped to the 20-row ceiling

    def test_search_discussion_requires_query(self, fresh_db):
        from agent.tools import dispatch

        result = json.loads(dispatch("search_discussion", {}, {}))
        assert "error" in result
        assert "KeyError" in result["error"]
