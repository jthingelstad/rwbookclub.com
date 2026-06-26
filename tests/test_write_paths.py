"""Regression tests for Oliver's corpus write paths (DB-authoritative, no git commits)."""

from __future__ import annotations

import json


def test_review_is_db_backed_and_survives_regen(reset_books_cache):
    """A review must land in club_reviews (not just a markdown file) so a full corpus
    regen + prune — which now runs at startup — doesn't silently delete it."""
    from agent import corpus_gen, db
    from agent.club import reviews
    from corpus.paths import DATA_DIR

    res = reviews.write_review("enshittification", "Brad", rating="4", review="Sharp.")
    assert res["updated"] is False
    review_path = DATA_DIR / "reviews" / "enshittification--brad.md"
    assert review_path.exists()

    with db.connect() as conn:
        row = conn.execute(
            "SELECT r.rating FROM club_reviews r JOIN club_books b ON b.id = r.book_id "
            "JOIN club_members m ON m.id = r.member_id WHERE b.slug = ? AND m.slug = ?",
            ("enshittification", "brad"),
        ).fetchone()
    assert row is not None and row["rating"] == 4

    corpus_gen.generate()  # full regen + prune (what startup does)
    assert review_path.exists()  # survived because it's DB-backed now


def test_review_update_preserves_id_and_created_at(reset_books_cache):
    """Editing a review keeps its club_reviews id / airtable_id / created_at (the 'preserve
    id+createdAt on update' contract), and updates the mutable fields."""
    from agent import clubdb, db
    from agent.club import reviews

    reviews.write_review("being-mortal", "Brad", rating="3")
    with db.connect() as conn:
        bid = clubdb.book_id_for_slug(conn, "being-mortal")
        mid = clubdb.member_id_for_slug(conn, "brad")
        before = conn.execute(
            "SELECT id, airtable_id, created_at, rating FROM club_reviews "
            "WHERE book_id = ? AND member_id = ?", (bid, mid)).fetchone()

    res = reviews.write_review("being-mortal", "Brad", rating="5", review="Changed my mind.")
    assert res["updated"] is True
    with db.connect() as conn:
        after = conn.execute(
            "SELECT id, airtable_id, created_at, rating, body FROM club_reviews "
            "WHERE book_id = ? AND member_id = ?", (bid, mid)).fetchone()
    assert after["id"] == before["id"]
    assert after["airtable_id"] == before["airtable_id"]
    assert after["created_at"] == before["created_at"]
    assert after["rating"] == 5 and after["body"] == "Changed my mind."


def test_add_book_creates_missing_author_records(monkeypatch, tmp_path):
    from agent import corpus_write

    data_dir = tmp_path / "data"
    (data_dir / "books").mkdir(parents=True)
    monkeypatch.setattr(corpus_write, "DATA_DIR", data_dir)

    corpus_write.write_book({"title": "New Book", "authors": ["New Author"]})

    assert (data_dir / "books" / "new-book.json").exists()
    author = json.loads((data_dir / "authors" / "new-author.json").read_text())
    # Normalized shape: bio is omitted (not null) until one is set — matches every
    # existing bio-less author file the generator reproduces.
    assert author == {"name": "New Author"}
