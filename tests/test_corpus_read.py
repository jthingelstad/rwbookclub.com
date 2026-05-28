"""Corpus read layer — parse_frontmatter, find_books scoring, upcoming filter, cache."""

from __future__ import annotations

import pytest


# ── parse_frontmatter ────────────────────────────────────────────────────────
class TestParseFrontmatter:
    def test_yaml_frontmatter(self):
        from agent.corpus_read import parse_frontmatter
        text = "---\nname: Erik\nrating: 5\n---\nBody text here."
        fm, body = parse_frontmatter(text)
        assert fm == {"name": "Erik", "rating": 5}
        assert body == "Body text here."

    def test_no_frontmatter(self):
        from agent.corpus_read import parse_frontmatter
        fm, body = parse_frontmatter("Just body, no frontmatter.")
        assert fm == {}
        assert body == "Just body, no frontmatter."

    def test_empty_body(self):
        from agent.corpus_read import parse_frontmatter
        fm, body = parse_frontmatter("---\nkey: value\n---\n")
        assert fm == {"key": "value"}
        assert body == ""


# ── upcoming_meetings date filter (T1.4) ─────────────────────────────────────
class TestUpcomingMeetingsFilter:
    def test_all_returned_are_future(self):
        """T1.4 regression: past placeholders must be filtered out."""
        from datetime import datetime, timezone
        from agent.corpus_read import upcoming_meetings

        today_iso = datetime.now(timezone.utc).date().isoformat()
        for m in upcoming_meetings():
            md = (m.get("meetingDate") or "")[:10]
            assert md >= today_iso, f"past placeholder leaked: {m['title']} {md}"


# ── books() cache (T2.12) ────────────────────────────────────────────────────
class TestBooksCache:
    def test_cache_hit_returns_same_object(self, reset_books_cache):
        from agent import corpus_read as cr
        first = cr.books()
        second = cr.books()
        # Cache hit returns the SAME list object, not a fresh build.
        assert first is second

    def test_cache_invalidates_on_file_touch(self, reset_books_cache, tmp_path):
        import pathlib, time
        from agent import corpus_read as cr

        first = cr.books()
        first_sig = cr._books_cache_sig
        # Touch a corpus file to bump mtime.
        target = next(iter(pathlib.Path("corpus/data/books").glob("*.json")))
        original_mtime = target.stat().st_mtime
        try:
            time.sleep(0.01)
            target.touch()
            second_sig = cr._books_signature()
            assert second_sig != first_sig
            cr.books()  # triggers rebuild
            assert cr._books_cache_sig == second_sig
        finally:
            import os
            os.utime(target, (original_mtime, original_mtime))


# ── find_books scoring ──────────────────────────────────────────────────────
class TestFindBooks:
    def test_empty_query_returns_empty(self):
        from agent.corpus_read import find_books
        assert find_books("") == []
        assert find_books("   ") == []

    def test_known_author_exact_match(self):
        """Exact author match should rank a book first."""
        from agent.corpus_read import find_books
        results = find_books("Michael Pollan")
        assert results, "expected Pollan books in the corpus"
        # Every top hit should be a Pollan book.
        assert "Michael Pollan" in (results[0].get("authors") or [])

    def test_topic_substring(self):
        """Word matching a topic should surface books in that topic."""
        from agent.corpus_read import find_books
        results = find_books("technology")
        # Should find at least some Technology-topic books.
        assert any(b.get("topic") == "Technology" for b in results)

    def test_subjects_token_fallback(self):
        """T2.12 verification — multi-word query that misses titles should still
        surface books via the OL subject-tag token fallback."""
        from agent.corpus_read import find_books
        results = find_books("urban planning")
        # Triumph of the City has subject 'Urban economics' — the 'urban' token
        # should match through the subjects fallback.
        titles = {b.get("title") for b in results}
        assert "Triumph of the City" in titles


# ── _norm ────────────────────────────────────────────────────────────────────
class TestNorm:
    def test_lowercases(self):
        from agent.corpus_read import _norm
        assert _norm("HELLO") == "hello"

    def test_strips(self):
        from agent.corpus_read import _norm
        assert _norm("  hi  ") == "hi"

    def test_none(self):
        from agent.corpus_read import _norm
        assert _norm(None) == ""

    def test_empty(self):
        from agent.corpus_read import _norm
        assert _norm("") == ""
