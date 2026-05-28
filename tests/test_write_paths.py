"""Regression tests for Oliver's corpus write ordering."""

from __future__ import annotations

import json

import pytest


def test_review_sync_failure_does_not_write_file(monkeypatch, tmp_path):
    from agent import reviews
    from agent.gitwrite import GitWriteError

    reviews_dir = tmp_path / "reviews"
    monkeypatch.setattr(reviews, "REVIEWS_DIR", reviews_dir)
    monkeypatch.setattr(reviews.cr, "find_member", lambda _: {"slug": "jamie", "name": "Jamie"})
    monkeypatch.setattr(reviews.cr, "find_book", lambda _: {"slug": "book", "title": "Book"})

    def fail_sync():
        raise GitWriteError("sync failed")

    monkeypatch.setattr(reviews.gitwrite, "sync", fail_sync)

    with pytest.raises(GitWriteError):
        reviews.write_review("Book", "Jamie", rating="5")

    assert not (reviews_dir / "book--jamie.md").exists()


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
