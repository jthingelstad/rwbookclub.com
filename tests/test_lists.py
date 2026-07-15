"""Book lists — clubdb helpers, the lists.py writer (auth + corpus), the award→list
migration round-trip, and the _list_doc corpus shape.

The isolated DB fixture starts from the public-safe session snapshot, which already contains the
migrated "Books of the Year" club list. These tests exercise the live write path on top of it.
"""

from __future__ import annotations

import sqlite3

import pytest

from agent import clubdb, corpus_gen, db
from agent import corpus_read as cr
from agent.club import lists as lw

pytestmark = pytest.mark.usefixtures("fresh_db")


# ── clubdb helpers ───────────────────────────────────────────────────────────
def test_create_list_slug_uniqueness():
    """Member-list slugs are owner-prefixed; same name twice gets a -2 suffix; a club list
    slugs straight from the name."""
    with db.connect() as conn:
        jamie = clubdb.member_id_for_slug(conn, "jamie")
        a = clubdb.create_list(conn, name="My Favorites", scope="member", owner_id=jamie)
        b = clubdb.create_list(conn, name="My Favorites", scope="member", owner_id=jamie)
        c = clubdb.create_list(conn, name="My Favorites", scope="club", owner_id=None)
    assert a["slug"] == "jamie-my-favorites"
    assert b["slug"] == "jamie-my-favorites-2"
    assert c["slug"] == "my-favorites"


def test_set_list_book_upsert_and_remove():
    """set_list_book appends on first add (True) and updates the note in place on re-add (False),
    keeping the ordinal stable; remove_list_book reports whether a row went away."""
    with db.connect() as conn:
        jamie = clubdb.member_id_for_slug(conn, "jamie")
        lst = clubdb.create_list(conn, name="Stack", scope="member", owner_id=jamie)
        b1 = clubdb.book_id_for_slug(conn, "enshittification")
        b2 = clubdb.book_id_for_slug(conn, "heart-of-darkness")

        assert clubdb.set_list_book(conn, lst["id"], b1, "first") is True
        assert clubdb.set_list_book(conn, lst["id"], b2, None) is True
        # re-add b1 with a new note → in-place update, not a duplicate
        assert clubdb.set_list_book(conn, lst["id"], b1, "updated") is False

        rows = conn.execute(
            "SELECT book_id, ordinal, note FROM club_list_books WHERE list_id = ? ORDER BY ordinal",
            (lst["id"],),
        ).fetchall()
        assert [(r["book_id"], r["ordinal"], r["note"]) for r in rows] == [
            (b1, 0, "updated"), (b2, 1, None)]

        assert clubdb.remove_list_book(conn, lst["id"], b1) is True
        assert clubdb.remove_list_book(conn, lst["id"], b1) is False  # already gone


def test_all_lists_projects_owner_and_entries():
    with db.connect() as conn:
        jamie = clubdb.member_id_for_slug(conn, "jamie")
        lst = clubdb.create_list(conn, name="Owned", scope="member", owner_id=jamie,
                                 description="mine")
        clubdb.set_list_book(conn, lst["id"], clubdb.book_id_for_slug(conn, "heart-of-darkness"),
                             "note")
        all_lists = {x["slug"]: x for x in clubdb.all_lists(conn)}
    mine = all_lists["jamie-owned"]
    assert mine["owner_slug"] == "jamie"
    assert mine["description"] == "mine"
    assert mine["entries"] == [{"book_slug": "heart-of-darkness", "note": "note"}]
    # the migrated club list rides along, owner-less
    boty = all_lists["books-of-the-year"]
    assert boty["scope"] == "club" and boty["owner_slug"] is None


# ── _list_doc corpus shape ───────────────────────────────────────────────────
def test_list_doc_shape():
    member_doc = corpus_gen._list_doc({
        "name": "Faves", "scope": "member", "owner_slug": "jamie", "description": "x",
        "entries": [{"book_slug": "a", "note": "why"}, {"book_slug": "b", "note": None}],
    })
    assert member_doc == {
        "name": "Faves", "scope": "member", "owner": "jamie", "description": "x",
        "books": [{"book": "a", "note": "why"}, {"book": "b"}],  # empty note omitted
    }
    # club list: owner is None, description omitted when empty
    club_doc = corpus_gen._list_doc({
        "name": "Club", "scope": "club", "owner_slug": None, "description": None, "entries": [],
    })
    assert club_doc == {"name": "Club", "scope": "club", "owner": None, "books": []}
    assert "description" not in club_doc


# ── lists.py writer: create / add / remove / edit / delete ───────────────────
def test_writer_create_member_and_club_lists():
    member = lw.create_list("Beach Reads", "Summer picks", owner_slug="jamie", scope="member")
    assert member["slug"] == "jamie-beach-reads" and member["scope"] == "member"

    club = lw.create_list("Our Favorites", "The group's best", scope="club")
    assert club["slug"] == "our-favorites" and club["scope"] == "club"

    # the corpus file landed and round-trips through corpus_read
    found = cr.find_list("Our Favorites")
    assert found and found["slug"] == "our-favorites"
    assert found["owner"] is None


def test_writer_create_member_requires_linked_member():
    with pytest.raises(lw.ListError):
        lw.create_list("Ghost List", owner_slug="not-a-member", scope="member")


def test_writer_add_remove_book_with_note():
    lst = lw.create_list("Notable", "n", owner_slug="jamie", scope="member")

    added = lw.add_book(lst["slug"], "Heart of Darkness", "a haunting one",
                        actor_slug="jamie", is_admin=False)
    assert added["added"] is True and added["book"] == "Heart of Darkness"

    # re-add → note update, not a new entry
    again = lw.add_book(lst["slug"], "Heart of Darkness", "still haunting",
                        actor_slug="jamie", is_admin=False)
    assert again["added"] is False

    doc = cr.find_list(lst["slug"])
    assert doc["books"] == [{"book": "heart-of-darkness", "note": "still haunting"}]

    removed = lw.remove_book(lst["slug"], "Heart of Darkness", actor_slug="jamie", is_admin=False)
    assert removed["removed"] is True
    assert cr.find_list(lst["slug"])["books"] == []


def test_writer_reorder_books():
    lst = lw.create_list("Ranked", "r", owner_slug="jamie", scope="member")
    lw.add_book(lst["slug"], "Heart of Darkness", None, actor_slug="jamie", is_admin=False)
    lw.add_book(lst["slug"], "Enshittification", None, actor_slug="jamie", is_admin=False)
    order = [b["book"] for b in cr.find_list(lst["slug"])["books"]]

    lw.reorder(lst["slug"], list(reversed(order)), actor_slug="jamie", is_admin=False)
    assert [b["book"] for b in cr.find_list(lst["slug"])["books"]] == list(reversed(order))

    # a stale/partial order must not drop the unmentioned book
    lw.reorder(lst["slug"], [order[0]], actor_slug="jamie", is_admin=False)
    after = [b["book"] for b in cr.find_list(lst["slug"])["books"]]
    assert set(after) == set(order) and after[0] == order[0]


def test_writer_set_note():
    lst = lw.create_list("Noted", "n", owner_slug="jamie", scope="member")
    lw.add_book(lst["slug"], "Heart of Darkness", "first", actor_slug="jamie", is_admin=False)

    lw.set_note(lst["slug"], "Heart of Darkness", "updated", actor_slug="jamie", is_admin=False)
    assert cr.find_list(lst["slug"])["books"] == [{"book": "heart-of-darkness", "note": "updated"}]

    lw.set_note(lst["slug"], "Heart of Darkness", "", actor_slug="jamie", is_admin=False)
    assert cr.find_list(lst["slug"])["books"] == [{"book": "heart-of-darkness"}]

    with pytest.raises(lw.ListError):
        lw.set_note(lst["slug"], "Enshittification", "x", actor_slug="jamie", is_admin=False)


def test_writer_edit_and_delete():
    lst = lw.create_list("Temp", "temp desc", owner_slug="jamie", scope="member")
    lw.edit_list(lst["slug"], description="new desc", actor_slug="jamie", is_admin=False)
    assert cr.find_list(lst["slug"])["description"] == "new desc"

    lw.delete_list(lst["slug"], actor_slug="jamie", is_admin=False)
    assert cr.find_list(lst["slug"]) is None


# ── authorization ────────────────────────────────────────────────────────────
def test_member_cannot_touch_club_list():
    club = lw.create_list("Club Picks", "g", scope="club")
    with pytest.raises(lw.ListError, match="admin"):
        lw.add_book(club["slug"], "Heart of Darkness", None, actor_slug="jamie", is_admin=False)


def test_member_cannot_touch_another_members_list():
    jamies = lw.create_list("Jamie Only", "j", owner_slug="jamie", scope="member")
    with pytest.raises(lw.ListError, match="your"):
        lw.add_book(jamies["slug"], "Heart of Darkness", None, actor_slug="erik", is_admin=False)


def test_admin_can_touch_any_list():
    club = lw.create_list("Admin Club", "g", scope="club")
    res = lw.add_book(club["slug"], "Heart of Darkness", None, actor_slug="jamie", is_admin=True)
    assert res["added"] is True

    jamies = lw.create_list("Jamie List", "j", owner_slug="jamie", scope="member")
    res2 = lw.add_book(jamies["slug"], "Enshittification", None, actor_slug="erik", is_admin=True)
    assert res2["added"] is True


# ── award → list migration round-trip ────────────────────────────────────────
def test_award_to_list_migration_round_trip(tmp_path):
    """Seed the legacy 3 award tables in a scratch DB, run _migrate_club, and confirm the one
    award became the "Books of the Year" club list (note = year), the award tables are gone, and
    foreign keys are clean."""
    conn = sqlite3.connect(tmp_path / "legacy.db")
    conn.row_factory = sqlite3.Row
    # The current schema (creates club_lists + club_list_books and the rest, IF NOT EXISTS).
    conn.executescript(clubdb.CLUB_SCHEMA)
    # Re-introduce the retired award tables and a single Book-of-the-Year record.
    conn.executescript(
        "CREATE TABLE club_awards (id INTEGER PRIMARY KEY, name TEXT, year INTEGER, "
        "award_category TEXT, notes TEXT);\n"
        "CREATE TABLE club_award_books (award_id INTEGER, book_id INTEGER, "
        "PRIMARY KEY (award_id, book_id));\n"
        "CREATE TABLE club_award_voters (award_id INTEGER, member_id INTEGER, "
        "PRIMARY KEY (award_id, member_id));\n"
        "INSERT INTO club_books (id, slug, title) VALUES (69, 'american-nations', 'American Nations');\n"
        "INSERT INTO club_awards (id, name, year, award_category) "
        "VALUES (1, 'Book of the Year', 2016, 'Book of the Year');\n"
        "INSERT INTO club_award_books (award_id, book_id) VALUES (1, 69);\n"
    )
    conn.commit()

    clubdb._migrate_club(conn)

    # award tables dropped
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not (tables & {"club_awards", "club_award_books", "club_award_voters"})

    # the BotY club list now holds American Nations with note "2016"
    lst = conn.execute("SELECT * FROM club_lists WHERE slug = 'books-of-the-year'").fetchone()
    assert lst["scope"] == "club" and lst["owner_id"] is None
    entry = conn.execute(
        "SELECT lb.book_id, lb.note FROM club_list_books lb WHERE lb.list_id = ?", (lst["id"],)
    ).fetchone()
    assert entry["book_id"] == 69 and entry["note"] == "2016"

    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    # idempotent: a second run is a no-op (no award tables left to migrate, no dup list)
    clubdb._migrate_club(conn)
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM club_lists WHERE slug = 'books-of-the-year'"
    ).fetchone()["c"] == 1
    conn.close()
