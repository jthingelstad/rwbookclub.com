"""Member identities: the new `website` surface, member-scoped removal + email protection, migration.

Discord/email/sms helpers are exercised elsewhere (test_db, test_tools_dispatch); this focuses on the
website surface and the removal rules introduced with member self-service.
"""

from __future__ import annotations

import pytest

from agent import db, identities


def test_identity_capability_is_not_reexported_by_database_facade():
    removed = {
        "link_member_identity",
        "member_slug_for_user",
        "link_member_email",
        "member_slug_for_email",
        "email_for_member",
        "link_member_sms",
        "link_member_website",
    }

    assert all(not hasattr(db, name) for name in removed)


class TestWebsiteSurface:
    def test_link_normalizes_and_lists(self, fresh_db):
        identities.link_member_website("tomeri.org", "jamie")            # no scheme → https://
        identities.link_member_website("https://example.com/", "jamie")  # trailing slash dropped
        assert set(identities.websites_for_member("jamie")) == {"https://tomeri.org", "https://example.com"}
        listed = {(r["member_slug"], r["url"]) for r in identities.list_member_websites()}
        assert ("jamie", "https://tomeri.org") in listed
        assert ("jamie", "https://example.com") in listed

    def test_invalid_url_rejected(self, fresh_db):
        with pytest.raises(ValueError):
            identities.link_member_website("not a url", "jamie")

    def test_relink_same_url_is_idempotent(self, fresh_db):
        identities.link_member_website("https://a.example", "jamie")
        identities.link_member_website("https://a.example", "jamie")
        assert identities.websites_for_member("jamie") == ["https://a.example"]

    def test_websites_are_per_member(self, fresh_db):
        identities.link_member_website("https://jamie.example", "jamie")
        identities.link_member_website("https://tom.example", "tom")
        assert identities.websites_for_member("jamie") == ["https://jamie.example"]
        assert identities.websites_for_member("tom") == ["https://tom.example"]


class TestRemoval:
    def test_remove_website_and_phone(self, fresh_db):
        identities.link_member_website("https://a.example", "jamie")
        identities.link_member_sms("612-555-1212", "jamie")
        assert identities.remove_member_website("https://a.example/", "jamie") is True   # normalize matches
        assert identities.websites_for_member("jamie") == []
        assert identities.remove_member_sms("(612) 555-1212", "jamie") is True           # same normalized form
        assert identities.sms_for_member("jamie") == []

    def test_email_can_never_be_removed(self, fresh_db):
        identities.link_member_email("jamie@example.test", "jamie")
        with pytest.raises(ValueError):
            identities.unlink_member_identity("email", "jamie@example.test", "jamie")
        assert identities.emails_for_member("jamie") == ["jamie@example.test"]

    def test_removal_is_member_scoped(self, fresh_db):
        identities.link_member_website("https://jamie.example", "jamie")
        # Tom can't remove Jamie's website.
        assert identities.unlink_member_identity("website", "https://jamie.example", "tom") is False
        assert identities.websites_for_member("jamie") == ["https://jamie.example"]

    def test_remove_missing_returns_false(self, fresh_db):
        assert identities.remove_member_website("https://nope.example", "jamie") is False


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
        assert identities.websites_for_member("jamie") == ["https://jamie.example"]

    def test_migration_is_idempotent_without_the_column(self, fresh_db):
        # No website column (the post-migration / fresh shape) → guard returns, no error.
        with fresh_db.connect() as conn:
            fresh_db.migrate_website_to_identities(conn)
