"""The unified club event log + the meeting_member_status projection.

Covers the write entrypoint (record_event + wrappers), the projection bumps
("stop asking once confirmed"), the derived roll-call/group reads, the
member timeline, and the one-time migration off the four legacy tables.
"""

from __future__ import annotations

import json

import pytest


def _ids():
    from agent import clubdb
    mid = clubdb.meeting_id_for_book_slug("a-world-appears")
    jamie = clubdb.lookup_member_id("jamie")
    tom = clubdb.lookup_member_id("tom")
    return mid, jamie, tom


class TestRecordEvent:
    def test_event_inserts_and_projects_attendance(self, fresh_db):
        mid, jamie, _ = _ids()
        fresh_db.record_attendance_report(mid, jamie, "yes", surface="discord")
        row = fresh_db.meeting_member_status(mid, jamie)
        assert row["attendance"] == "yes"
        assert row["attendance_answered_at"]
        events = fresh_db.meeting_events(mid, kind="attendance_reported")
        assert events[0]["category"] == "meeting_ops"
        assert events[0]["detail"] == "yes"

    def test_reading_report_projects_value_and_json_detail(self, fresh_db):
        mid, jamie, _ = _ids()
        fresh_db.record_reading_report(mid, jamie, "on_track", progress="halfway",
                                       page=120, percent=50, surface="email")
        row = fresh_db.meeting_member_status(mid, jamie)
        assert row["reading"] == "on_track"
        assert row["reading_page"] == 120
        assert row["reading_percent"] == 50
        detail = json.loads(fresh_db.meeting_events(mid, kind="reading_reported")[0]["detail"])
        assert detail["status"] == "on_track" and detail["progress"] == "halfway"

    def test_requests_bump_ask_counts(self, fresh_db):
        mid, jamie, _ = _ids()
        fresh_db.record_attendance_request(mid, jamie)
        fresh_db.record_attendance_request(mid, jamie)
        fresh_db.record_reading_request(mid, jamie)
        row = fresh_db.meeting_member_status(mid, jamie)
        assert row["attendance_asks"] == 2
        assert row["reading_asks"] == 1
        assert row["last_asked_at"]
        assert row["reading_last_asked_at"]

    def test_reported_is_last_write_wins(self, fresh_db):
        mid, jamie, _ = _ids()
        fresh_db.record_attendance_report(mid, jamie, "unsure")
        fresh_db.record_attendance_report(mid, jamie, "yes")
        assert fresh_db.meeting_member_status(mid, jamie)["attendance"] == "yes"
        # both reports are kept in the timeline, newest first.
        assert [e["detail"] for e in fresh_db.meeting_events(mid, kind="attendance_reported")] == ["yes", "unsure"]

    def test_projection_kinds_require_both_ids(self, fresh_db):
        with pytest.raises(ValueError):
            fresh_db.record_event(actor="member", kind="attendance_reported", member_id=1)
        with pytest.raises(ValueError):
            fresh_db.record_event(actor="member", kind="attendance_reported", meeting_id=1)

    def test_attendance_validation(self, fresh_db):
        mid, jamie, _ = _ids()
        with pytest.raises(ValueError):
            fresh_db.record_attendance_report(mid, jamie, "maybe")


class TestStopAsking:
    def test_unknown_until_answered_then_known(self, fresh_db):
        mid, jamie, _ = _ids()
        # No row → unknown (a missing projection row is the "pending/keep asking" signal).
        assert fresh_db.meeting_member_status(mid, jamie) is None
        fresh_db.record_attendance_report(mid, jamie, "yes")
        # Answered → attendance != 'unknown' is the scheduler's "stop asking" predicate.
        assert fresh_db.meeting_member_status(mid, jamie)["attendance"] == "yes"

    def test_finished_reading_is_terminal_signal(self, fresh_db):
        mid, jamie, _ = _ids()
        fresh_db.record_reading_report(mid, jamie, "finished")
        assert fresh_db.meeting_member_status(mid, jamie)["reading"] == "finished"


class TestRollCallDerivation:
    def test_open_close_cycle(self, fresh_db):
        mid, _, _ = _ids()
        assert fresh_db.current_roll_call(mid) is None
        assert not fresh_db.has_open_roll_call(mid)
        fresh_db.record_group_event(mid, "roll_call_opened",
                                    detail={"channel_id": "c1", "message_id": "m1", "opened_by": "scheduler"})
        rc = fresh_db.current_roll_call(mid)
        assert rc["status"] == "open" and rc["channel_id"] == "c1" and rc["message_id"] == "m1"
        assert fresh_db.has_open_roll_call(mid)
        fresh_db.record_group_event(mid, "roll_call_closed", actor="admin")
        assert fresh_db.current_roll_call(mid)["status"] == "closed"
        assert not fresh_db.has_open_roll_call(mid)

    def test_has_group_event(self, fresh_db):
        mid, _, _ = _ids()
        assert not fresh_db.has_group_event(mid, "week_reminder_sent")
        fresh_db.record_group_event(mid, "week_reminder_sent", surface="email")
        assert fresh_db.has_group_event(mid, "week_reminder_sent")


class TestTimeline:
    def test_events_for_member_orders_by_occurred_at(self, fresh_db):
        mid, jamie, _ = _ids()
        fresh_db.record_attendance_report(mid, jamie, "yes")
        fresh_db.record_event(actor="member", kind="email_reply", member_id=jamie,
                              meeting_id=mid, detail="hello", surface="email")
        timeline = fresh_db.events_for_member(jamie)
        kinds = {e["kind"] for e in timeline}
        assert {"attendance_reported", "email_reply"} <= kinds

    def test_occurred_at_distinct_from_created_at(self, fresh_db):
        mid, _, _ = _ids()
        fresh_db.record_meeting_scheduled(mid, occurred_at="2030-01-01",
                                          detail={"date": "2030-01-01"})
        ev = fresh_db.meeting_events(mid, kind="meeting_scheduled")[0]
        assert str(ev["occurred_at"]).startswith("2030-01-01")
        assert not str(ev["created_at"]).startswith("2030-01-01")
        assert ev["category"] == "meeting"


class TestMigration:
    def test_migration_seeds_projection_and_backfills_events(self, fresh_db):
        from agent import clubdb
        db = fresh_db
        mid = clubdb.meeting_id_for_book_slug("a-world-appears")
        jamie = clubdb.lookup_member_id("jamie")
        tom = clubdb.lookup_member_id("tom")
        with db.connect() as conn:
            # Recreate the four legacy tables and seed a representative slice.
            conn.executescript("""
                CREATE TABLE meeting_attendance (
                    meeting_id INTEGER, member_id INTEGER, status TEXT,
                    responded_at TEXT, source TEXT,
                    PRIMARY KEY (meeting_id, member_id));
                CREATE TABLE reading_statuses (
                    meeting_id INTEGER, member_id INTEGER, status TEXT, progress TEXT,
                    page INTEGER, percent INTEGER, updated_at TEXT, source TEXT,
                    PRIMARY KEY (meeting_id, member_id));
                CREATE TABLE member_contacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, meeting_id INTEGER, member_id INTEGER,
                    kind TEXT, surface TEXT, direction TEXT, status TEXT, subject TEXT,
                    created_at TEXT);
                CREATE TABLE roll_calls (
                    meeting_id INTEGER PRIMARY KEY, channel_id TEXT, message_id TEXT,
                    opened_by TEXT, opened_at TEXT, status TEXT, closed_at TEXT);
            """)
            conn.execute("INSERT INTO meeting_attendance VALUES (?,?,?,?,?)",
                         (mid, jamie, "yes", "2026-06-10 09:00:00", "email"))
            conn.execute(
                "INSERT INTO reading_statuses VALUES (?,?,?,?,?,?,?,?)",
                (mid, jamie, "on_track", "halfway", 120, 50, "2026-06-11 10:00:00", "email"))
            # Two delivered roll-call asks to Tom (never answered) + one inbound reply.
            conn.execute("INSERT INTO member_contacts (meeting_id, member_id, kind, surface, direction, status, subject, created_at) "
                         "VALUES (?,?,'roll_call','email','outbound','sent','Roll call','2026-06-09 08:00:00')", (mid, tom))
            conn.execute("INSERT INTO member_contacts (meeting_id, member_id, kind, surface, direction, status, subject, created_at) "
                         "VALUES (?,?,'roll_call','email','outbound','sent','Roll call','2026-06-12 08:00:00')", (mid, tom))
            conn.execute("INSERT INTO member_contacts (meeting_id, member_id, kind, surface, direction, status, subject, created_at) "
                         "VALUES (?,?,'email_reply','email','inbound','received','Re: Roll call','2026-06-13 08:00:00')", (mid, jamie))
            conn.execute("INSERT INTO roll_calls VALUES (?,?,?,?,?,?,?)",
                         (mid, "ch1", "msg1", "scheduler", "2026-06-08 08:00:00", "closed", "2026-06-20 08:00:00"))
            conn.commit()
            db.migrate_meeting_events(conn)

        # Projection: preserved status + ask counts from delivered outbound contacts.
        jrow = db.meeting_member_status(mid, jamie)
        assert jrow["attendance"] == "yes" and jrow["reading"] == "on_track"
        assert jrow["reading_page"] == 120
        trow = db.meeting_member_status(mid, tom)
        assert trow["attendance"] == "unknown" and trow["attendance_asks"] == 2

        # Event history backfilled across all four sources.
        kinds = {e["kind"] for e in db.meeting_events(mid)}
        assert {"attendance_reported", "reading_reported", "attendance_requested",
                "email_reply", "roll_call_opened", "roll_call_closed"} <= kinds
        assert db.current_roll_call(mid)["status"] == "closed"

        # The four legacy tables are gone, FK check clean (FK check is asserted inside the migration).
        with db.connect() as conn:
            for t in ("meeting_attendance", "reading_statuses", "member_contacts", "roll_calls"):
                assert not db._table_exists(conn, t)

    def test_migration_is_a_noop_without_legacy_tables(self, fresh_db):
        # No member_contacts table present (the live shape post-migration) → guard returns early.
        with fresh_db.connect() as conn:
            fresh_db.migrate_meeting_events(conn)
