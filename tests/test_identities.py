"""Member identities: the new `website` surface, member-scoped removal + email protection, migration.

Discord/email/sms helpers are exercised elsewhere (test_db, test_tools_dispatch); this focuses on the
website surface and the removal rules introduced with member self-service.
"""

from __future__ import annotations

import pytest


class TestWebsiteSurface:
    def test_link_normalizes_and_lists(self, fresh_db):
        db = fresh_db
        db.link_member_website("tomeri.org", "jamie")            # no scheme → https://
        db.link_member_website("https://example.com/", "jamie")  # trailing slash dropped
        assert set(db.websites_for_member("jamie")) == {"https://tomeri.org", "https://example.com"}
        listed = {(r["member_slug"], r["url"]) for r in db.list_member_websites()}
        assert ("jamie", "https://tomeri.org") in listed
        assert ("jamie", "https://example.com") in listed

    def test_invalid_url_rejected(self, fresh_db):
        with pytest.raises(ValueError):
            fresh_db.link_member_website("not a url", "jamie")

    def test_relink_same_url_is_idempotent(self, fresh_db):
        db = fresh_db
        db.link_member_website("https://a.example", "jamie")
        db.link_member_website("https://a.example", "jamie")
        assert db.websites_for_member("jamie") == ["https://a.example"]

    def test_websites_are_per_member(self, fresh_db):
        db = fresh_db
        db.link_member_website("https://jamie.example", "jamie")
        db.link_member_website("https://tom.example", "tom")
        assert db.websites_for_member("jamie") == ["https://jamie.example"]
        assert db.websites_for_member("tom") == ["https://tom.example"]


class TestRemoval:
    def test_remove_website_and_phone(self, fresh_db):
        db = fresh_db
        db.link_member_website("https://a.example", "jamie")
        db.link_member_sms("612-555-1212", "jamie")
        assert db.remove_member_website("https://a.example/", "jamie") is True   # normalize matches
        assert db.websites_for_member("jamie") == []
        assert db.remove_member_sms("(612) 555-1212", "jamie") is True           # same normalized form
        assert db.sms_for_member("jamie") == []

    def test_email_can_never_be_removed(self, fresh_db):
        db = fresh_db
        db.link_member_email("jamie@example.test", "jamie")
        with pytest.raises(ValueError):
            db.unlink_member_identity("email", "jamie@example.test", "jamie")
        assert db.emails_for_member("jamie") == ["jamie@example.test"]

    def test_removal_is_member_scoped(self, fresh_db):
        db = fresh_db
        db.link_member_website("https://jamie.example", "jamie")
        # Tom can't remove Jamie's website.
        assert db.unlink_member_identity("website", "https://jamie.example", "tom") is False
        assert db.websites_for_member("jamie") == ["https://jamie.example"]

    def test_remove_missing_returns_false(self, fresh_db):
        assert fresh_db.remove_member_website("https://nope.example", "jamie") is False


class TestMigration:
    def test_website_column_folds_into_identities(self, fresh_db):
        from agent import clubdb
        db = fresh_db
        jamie = clubdb.lookup_member_id("jamie")
        with db.connect() as conn:
            # Recreate the pre-migration shape (the column was dropped from CLUB_SCHEMA).
            conn.execute("ALTER TABLE club_members ADD COLUMN website TEXT")
            conn.execute("UPDATE club_members SET website = 'https://jamie.example' WHERE id = ?", (jamie,))
            conn.commit()
            db.migrate_website_to_identities(conn)
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(club_members)")}
            assert "website" not in cols
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.websites_for_member("jamie") == ["https://jamie.example"]

    def test_migration_is_idempotent_without_the_column(self, fresh_db):
        # No website column (the post-migration / fresh shape) → guard returns, no error.
        with fresh_db.connect() as conn:
            fresh_db.migrate_website_to_identities(conn)
