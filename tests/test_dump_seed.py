"""Value escaping + CR normalization in the club_seed fixture dumper."""

from __future__ import annotations

from agent.script.dump_club_seed import _lit


def test_lit_escapes_single_quotes():
    assert _lit("O'Brien") == "'O''Brien'"


def test_lit_normalizes_cr_to_lf():
    # CR bytes would be stripped by git on commit → non-idempotent regen; the dumper kills them.
    assert "\r" not in _lit("line1\r\nline2\rline3")
    assert _lit("a\r\nb") == "'a\nb'"


def test_lit_none_and_numbers():
    assert _lit(None) == "NULL"
    assert _lit(42) == "42"
    assert _lit(4.5) == "4.5"
