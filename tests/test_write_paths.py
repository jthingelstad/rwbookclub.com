"""Regression tests for Oliver's corpus write ordering."""

from __future__ import annotations

import json

import pytest


def test_review_sync_failure_does_not_write_file(monkeypatch):
    from agent.club import reviews
    from agent.gitwrite import GitWriteError
    from corpus.paths import DATA_DIR

    monkeypatch.setattr(reviews.cr, "find_member", lambda _: {"slug": "jamie", "name": "Jamie"})
    monkeypatch.setattr(reviews.cr, "find_book", lambda _: {"slug": "book", "title": "Book"})

    def fail_sync():
        raise GitWriteError("sync failed")

    # sync() runs before the DB upsert / file regen, so a failure must leave nothing behind.
    monkeypatch.setattr(reviews.gitwrite, "sync", fail_sync)

    with pytest.raises(GitWriteError):
        reviews.write_review("Book", "Jamie", rating="5")

    assert not (DATA_DIR / "reviews" / "book--jamie.md").exists()


def test_review_is_db_backed_and_survives_regen(monkeypatch, reset_books_cache):
    """A review must land in club_reviews (not just a markdown file) so a full corpus
    regen + prune — which now runs at startup — doesn't silently delete it."""
    from agent import corpus_gen, db
    from agent.club import reviews
    from corpus.paths import DATA_DIR

    monkeypatch.setattr(reviews.gitwrite, "sync", lambda: None)
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


def test_add_book_sync_failure_does_not_write_file(monkeypatch, tmp_path):
    from agent import corpus_write
    from agent.gitwrite import GitWriteError

    data_dir = tmp_path / "data"
    (data_dir / "books").mkdir(parents=True)
    monkeypatch.setattr(corpus_write, "DATA_DIR", data_dir)

    def fail_sync():
        raise GitWriteError("sync failed")

    monkeypatch.setattr(corpus_write.gitwrite, "sync", fail_sync)

    with pytest.raises(GitWriteError):
        corpus_write.write_book({"title": "New Book"})

    assert not (data_dir / "books" / "new-book.json").exists()


def test_add_book_creates_missing_author_records(monkeypatch, tmp_path):
    from agent import corpus_write

    data_dir = tmp_path / "data"
    (data_dir / "books").mkdir(parents=True)
    monkeypatch.setattr(corpus_write, "DATA_DIR", data_dir)
    monkeypatch.setattr(corpus_write.gitwrite, "sync", lambda: None)
    monkeypatch.setattr(corpus_write.gitwrite, "commit_paths", lambda *_args, **_kwargs: "sha")

    corpus_write.write_book({"title": "New Book", "authors": ["New Author"]})

    assert (data_dir / "books" / "new-book.json").exists()
    author = json.loads((data_dir / "authors" / "new-author.json").read_text())
    # Normalized shape: bio is omitted (not null) until one is set — matches every
    # existing bio-less author file the generator reproduces.
    assert author == {"name": "New Author"}


def test_schedule_sync_failure_does_not_touch_book(monkeypatch, tmp_path):
    from agent import corpus_write
    from agent.gitwrite import GitWriteError

    data_dir = tmp_path / "data"
    books_dir = data_dir / "books"
    meetings_dir = data_dir / "meetings"
    books_dir.mkdir(parents=True)
    meetings_dir.mkdir()
    book_path = books_dir / "book.json"
    original = {"bookId": 1, "title": "Book", "picker": ["erik"]}
    book_path.write_text(json.dumps(original) + "\n")
    monkeypatch.setattr(corpus_write, "DATA_DIR", data_dir)

    def fail_sync():
        raise GitWriteError("sync failed")

    monkeypatch.setattr(corpus_write.gitwrite, "sync", fail_sync)
    monkeypatch.setattr(corpus_write.cr, "find_book", lambda _: {"slug": "book", "title": "Book"})
    monkeypatch.setattr(corpus_write.cr, "find_member", lambda _: {"slug": "jamie", "name": "Jamie"})

    with pytest.raises(GitWriteError):
        corpus_write.schedule_meeting("Book", "2026-07-01", "Jamie")

    assert json.loads(book_path.read_text()) == original
    assert list(meetings_dir.glob("*.json")) == []
