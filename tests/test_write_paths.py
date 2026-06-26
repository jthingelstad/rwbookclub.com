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
