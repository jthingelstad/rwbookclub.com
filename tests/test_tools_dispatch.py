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
