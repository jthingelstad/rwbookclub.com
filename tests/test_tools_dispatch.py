"""tools.dispatch error paths + a couple of happy paths."""

from __future__ import annotations

import json

from agent import clubdb


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
        fresh_db.link_member_email("jamie@thingelstad.com", "jamie")
        result = json.loads(dispatch("identity_status", {}, {
            "speaker_user_id": "u1",
            "member_slug": "jamie",
        }))
        assert result["speakerMemberSlug"] == "jamie"
        assert result["speakerMember"]["name"] == "Jamie"
        assert "jamie" in result["emailLinkedCurrentMembers"]

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
        rows = fresh_db.meeting_member_status_for_meeting(result["meetingStatus"]["meeting"]["meetingId"])
        assert rows[0]["member_slug"] == "jamie"
        assert rows[0]["attendance"] == "yes"

    def test_record_availability_saves_email_source(self, fresh_db):
        from agent.tools import dispatch

        result = json.loads(dispatch("record_availability", {"status": "no"}, {
            "speaker_user_id": "email:jamie@thingelstad.com",
            "member_slug": "jamie",
            "channel_id": "email:t1",
        }))
        assert result["saved"] is True
        mid = result["meetingStatus"]["meeting"]["meetingId"]
        events = fresh_db.meeting_events(mid, kind="attendance_reported")
        assert events[0]["surface"] == "email"
        assert events[0]["detail"] == "no"

    def test_record_reading_status_requires_linked_speaker(self, fresh_db):
        from agent.tools import dispatch

        result = json.loads(dispatch("record_reading_status", {"status": "on_track"}, {}))
        assert "error" in result

    def test_record_reading_status_saves_for_email_speaker(self, fresh_db):
        from agent.tools import dispatch

        result = json.loads(dispatch("record_reading_status", {
            "status": "on_track",
            "progress": "halfway",
            "percent": 50,
        }, {
            "speaker_user_id": "email:jamie@thingelstad.com",
            "member_slug": "jamie",
        }))
        assert result["saved"] is True
        meeting_id = result["readingStatus"]["meeting"]["meetingId"]
        row = fresh_db.meeting_member_status(meeting_id, clubdb.lookup_member_id("jamie"))
        assert row["reading"] == "on_track"
        events = fresh_db.meeting_events(meeting_id, kind="reading_reported")
        assert events[0]["surface"] == "email"

    def test_reading_status_returns_current_book(self, fresh_db):
        from agent.tools import dispatch

        result = json.loads(dispatch("reading_status", {}, {}))
        assert result["meeting"]["meetingKey"] == "a-world-appears"
        assert result["book"]["title"] == "A World Appears"

    def test_request_reading_update_requires_email_config_before_send(self, fresh_db):
        from agent.tools import dispatch

        fresh_db.link_member_email("jamie@thingelstad.com", "jamie")
        result = json.loads(dispatch("request_reading_update", {"member": "jamie"}, {
            "speaker_user_id": "u1",
            "member_slug": "jamie",
        }))
        assert result == {"error": "email is not configured"}

    def test_send_email_blocked_inside_inbound_email_channel(self, monkeypatch):
        from agent.mail import email_jmap
        from agent.tools import dispatch

        monkeypatch.setattr(email_jmap, "enabled", lambda: True)
        result = json.loads(dispatch("send_email", {
            "to": ["jamie@thingelstad.com"],
            "subject": "test",
            "body": "test",
        }, {"channel_id": "email:thread1"}))
        assert result == {
            "error": "inbound email replies are sent automatically; write response text instead"
        }

    def test_send_email_allows_linked_member_recipient(self, monkeypatch, fresh_db):
        from agent.mail import email_jmap
        from agent.tools import dispatch

        sent = []
        monkeypatch.setattr(email_jmap, "enabled", lambda: True)
        monkeypatch.setattr(email_jmap, "send_email", lambda **kwargs: sent.append(kwargs) or {
            "emailId": "e1",
            "to": kwargs["to"],
            "subject": kwargs["subject"],
        })
        fresh_db.link_member_email("jamie@thingelstad.com", "jamie")
        result = json.loads(dispatch("send_email", {
            "to": ["jamie@thingelstad.com"],
            "subject": "test",
            "body": "test",
        }, {"channel_id": "ch1"}))
        assert result["sent"] is True
        assert sent[0]["to"] == ["jamie@thingelstad.com"]

    def test_send_email_blocks_unknown_recipient(self, monkeypatch, fresh_db):
        from agent.mail import email_jmap
        from agent.tools import dispatch

        monkeypatch.setattr(email_jmap, "enabled", lambda: True)
        fresh_db.link_member_email("jamie@thingelstad.com", "jamie")
        result = json.loads(dispatch("send_email", {
            "to": ["outsider@example.test"],
            "subject": "test",
            "body": "test",
        }, {"channel_id": "ch1"}))
        assert result == {"error": "Oliver can only email linked book club member addresses from this tool"}

    def test_send_email_blocks_mailing_list_recipient(self, monkeypatch):
        from agent import config
        from agent.mail import email_jmap
        from agent.tools import dispatch

        monkeypatch.setattr(email_jmap, "enabled", lambda: True)
        result = json.loads(dispatch("send_email", {
            "to": [config.BOOK_CLUB_MAILING_LIST_ADDRESS],
            "subject": "test",
            "body": "test",
        }, {"channel_id": "ch1"}))
        assert result == {
            "error": (
                "the book club mailing list can only be emailed by approved meeting-cadence paths, "
                "not the general send_email tool"
            )
        }

    def test_request_reading_update_blocked_inside_inbound_email_channel(self, monkeypatch, fresh_db):
        from agent.mail import email_jmap
        from agent.tools import dispatch

        monkeypatch.setattr(email_jmap, "enabled", lambda: True)
        fresh_db.link_member_email("jamie@thingelstad.com", "jamie")
        result = json.loads(dispatch("request_reading_update", {"member": "jamie"}, {
            "channel_id": "email:thread1",
            "speaker_user_id": "email:jamie@thingelstad.com",
            "member_slug": "jamie",
        }))
        assert result == {"error": "email check-ins cannot be initiated from inbound email"}

    def test_request_roll_call_update_requires_email_config_before_send(self, fresh_db):
        from agent.tools import dispatch

        fresh_db.link_member_email("jamie@thingelstad.com", "jamie")
        result = json.loads(dispatch("request_roll_call_update", {"member": "jamie"}, {
            "speaker_user_id": "email:jamie@thingelstad.com",
            "member_slug": "jamie",
        }))
        assert result == {"error": "email is not configured"}

    def test_request_roll_call_update_sends_to_linked_current_members(self, monkeypatch, fresh_db):
        from agent import config
        from agent.mail import email_jmap
        from agent.tools import dispatch

        sent = []
        monkeypatch.setattr(email_jmap, "enabled", lambda: True)
        monkeypatch.setattr(email_jmap, "send_email", lambda **kwargs: sent.append(kwargs) or {
            "emailId": f"e{len(sent)}",
            "to": kwargs["to"],
            "subject": kwargs["subject"],
        })
        fresh_db.link_member_email("jamie@thingelstad.com", "jamie")
        fresh_db.link_member_email("tom@tomeri.org", "tom")
        result = json.loads(dispatch("request_roll_call_update", {}, {
            "speaker_user_id": str(config.ADMIN_USER_ID),
            "member_slug": "jamie",
            "channel_id": "ch1",
        }))
        assert len(result["sent"]) == 2
        assert {call["to"][0] for call in sent} == {"jamie@thingelstad.com", "tom@tomeri.org"}
        assert all("Roll call: A World Appears on 2026-06-30" == call["subject"] for call in sent)
        assert all("yes, no, or unsure" in call["body"] for call in sent)
        assert all("2026-06-30" in call["body"] for call in sent)

    def test_request_roll_call_update_skips_confirmed_members(self, monkeypatch, fresh_db):
        from agent import config
        from agent.mail import email_jmap
        from agent.club import meeting_rules
        from agent.tools import dispatch

        sent = []
        monkeypatch.setattr(email_jmap, "enabled", lambda: True)
        monkeypatch.setattr(email_jmap, "send_email", lambda **kwargs: sent.append(kwargs) or {
            "emailId": f"e{len(sent)}",
            "to": kwargs["to"],
            "subject": kwargs["subject"],
        })
        meeting = meeting_rules.next_meeting()
        fresh_db.record_attendance_report(
            meeting["meetingId"], clubdb.lookup_member_id("jamie"), "yes", surface="email")
        fresh_db.link_member_email("jamie@thingelstad.com", "jamie")
        fresh_db.link_member_email("tom@tomeri.org", "tom")
        result = json.loads(dispatch("request_roll_call_update", {}, {
            "speaker_user_id": str(config.ADMIN_USER_ID),
            "member_slug": "jamie",
            "channel_id": "ch1",
        }))
        assert {call["to"][0] for call in sent} == {"tom@tomeri.org"}
        assert result["skipped"] == [{"member": "jamie", "reason": "already yes"}]

    def test_request_roll_call_update_all_skipped_does_not_open_roll_call(self, monkeypatch, fresh_db):
        from agent import config
        from agent.mail import email_jmap
        from agent.club import meeting_rules
        from agent.tools import dispatch

        sent = []
        monkeypatch.setattr(email_jmap, "enabled", lambda: True)
        monkeypatch.setattr(email_jmap, "send_email", lambda **kwargs: sent.append(kwargs) or {
            "emailId": f"e{len(sent)}",
            "to": kwargs["to"],
            "subject": kwargs["subject"],
        })
        meeting = meeting_rules.next_meeting()
        fresh_db.record_attendance_report(meeting["meetingId"], clubdb.lookup_member_id("jamie"), "yes")
        fresh_db.link_member_email("jamie@thingelstad.com", "jamie")
        result = json.loads(dispatch("request_roll_call_update", {"member": "jamie"}, {
            "speaker_user_id": str(config.ADMIN_USER_ID),
            "member_slug": "jamie",
            "channel_id": "ch1",
        }))
        assert result["sent"] == []
        assert sent == []
        assert fresh_db.current_roll_call(meeting["meetingId"]) is None

    def test_request_roll_call_update_blocked_inside_inbound_email_channel(self, monkeypatch, fresh_db):
        from agent.mail import email_jmap
        from agent.tools import dispatch

        monkeypatch.setattr(email_jmap, "enabled", lambda: True)
        fresh_db.link_member_email("jamie@thingelstad.com", "jamie")
        result = json.loads(dispatch("request_roll_call_update", {"member": "jamie"}, {
            "channel_id": "email:thread1",
            "speaker_user_id": "email:jamie@thingelstad.com",
            "member_slug": "jamie",
        }))
        assert result == {"error": "roll-call emails cannot be initiated from inbound email"}

    def test_request_reading_update_skips_finished_member(self, monkeypatch, fresh_db):
        from agent.mail import email_jmap
        from agent.club import meeting_rules
        from agent.tools import dispatch

        monkeypatch.setattr(email_jmap, "enabled", lambda: True)
        fresh_db.link_member_email("jamie@thingelstad.com", "jamie")
        meeting = meeting_rules.next_meeting()
        fresh_db.record_reading_report(
            meeting["meetingId"], clubdb.lookup_member_id("jamie"), "finished", surface="email")
        result = json.loads(dispatch("request_reading_update", {"member": "jamie"}, {
            "speaker_user_id": "email:jamie@thingelstad.com",
            "member_slug": "jamie",
        }))
        assert result["sent"] is False
        assert "already marked finished" in result["reason"]

    def test_meeting_readiness_combines_attendance_and_reading(self, fresh_db):
        from agent.club import meeting_rules
        from agent.tools import dispatch

        meeting = meeting_rules.next_meeting()
        fresh_db.record_attendance_report(meeting["meetingId"], clubdb.lookup_member_id("jamie"), "yes")
        fresh_db.record_reading_report(meeting["meetingId"], clubdb.lookup_member_id("jamie"), "finished")
        result = json.loads(dispatch("meeting_readiness", {}, {}))
        assert result["counts"]["attending"] == 1
        assert result["counts"]["attendingAndFinished"] == 1
        assert result["counts"]["needsRollCall"] == 4
        assert result["needsReading"] == []

    def test_meeting_readiness_requires_picker_attendance(self, fresh_db):
        from agent import corpus_read
        from agent.club import meeting_rules
        from agent.tools import dispatch

        meeting = meeting_rules.next_meeting()
        pickers = set(meeting["pickerSlugs"])
        non_pickers = [
            m for m in corpus_read.members()
            if m.get("isCurrent") and m["slug"] not in pickers
        ]
        for member in non_pickers[:3]:
            fresh_db.record_attendance_report(
                meeting["meetingId"], clubdb.lookup_member_id(member["slug"]), "yes")
            fresh_db.record_reading_report(
                meeting["meetingId"], clubdb.lookup_member_id(member["slug"]), "finished")
        result = json.loads(dispatch("meeting_readiness", {}, {}))
        assert result["attendance"]["hasQuorum"] is True
        assert result["attendance"]["pickerAvailable"] is False
        assert result["ready"] is False

    def test_meeting_campaign_returns_recommended_actions_and_contacts(self, fresh_db):
        from agent.club import meeting_rules
        from agent.tools import dispatch

        meeting = meeting_rules.next_meeting()
        fresh_db.link_member_email("jamie@example.test", "jamie")
        fresh_db.record_reading_request(
            meeting["meetingId"], clubdb.lookup_member_id("jamie"), surface="email")
        result = json.loads(dispatch("meeting_campaign", {}, {}))
        jamie = next(m for m in result["members"] if m["memberSlug"] == "jamie")
        assert jamie["emailLinked"] is True
        assert jamie["readingCheckinCount"] == 1
        assert jamie["readingLastAskedAt"]
        assert result["recommendedActions"]

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
