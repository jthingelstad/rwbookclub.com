"""Rating + discussion-quality parsers — locked anchored after T1.5."""

from __future__ import annotations

import pytest

from agent.club.reviews import ReviewError, _parse_1to5, _parse_rating


class TestParseRating:
    @pytest.mark.parametrize("value,expected", [("1", 1), ("2", 2), ("3", 3), ("4", 4), ("5", 5)])
    def test_valid_digits(self, value, expected):
        rating, dnf = _parse_rating(value)
        assert rating == expected
        assert dnf is False

    @pytest.mark.parametrize("value", ["DNF", "dnf", "DidNotFinish", " DNF ", "Didn't Finish"])
    def test_dnf_variants(self, value):
        rating, dnf = _parse_rating(value)
        assert rating is None
        assert dnf is True

    def test_empty(self):
        rating, dnf = _parse_rating("")
        assert rating is None
        assert dnf is False

    def test_none(self):
        rating, dnf = _parse_rating(None)
        assert rating is None
        assert dnf is False

    @pytest.mark.parametrize("value", ["11", "5stars", "5/5", "rating: 4", "0", "6", "five"])
    def test_anchored_rejects(self, value):
        # The T1.5 fix: these used to silently parse as their leading digit.
        with pytest.raises(ReviewError):
            _parse_rating(value)


class TestParse1to5:
    @pytest.mark.parametrize("value,expected", [("1", 1), ("3", 3), ("5", 5)])
    def test_valid(self, value, expected):
        assert _parse_1to5(value) == expected

    def test_empty(self):
        assert _parse_1to5("") is None
        assert _parse_1to5(None) is None

    @pytest.mark.parametrize("value", ["11", "5stars", "abc", "0"])
    def test_anchored_rejects(self, value):
        with pytest.raises(ReviewError):
            _parse_1to5(value)
