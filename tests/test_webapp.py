"""Web-app spike: token mint/resolve (expiry, unknown, member join)."""

from __future__ import annotations

from datetime import timedelta

from agent import clubdb, webapp


def _jamie_id() -> int:
    mid = clubdb.lookup_member_id("jamie")
    assert mid is not None
    return mid


def test_mint_and_resolve_round_trip():
    token = webapp.mint_token(_jamie_id(), is_admin=True)
    member = webapp.resolve_token(token)
    assert member is not None
    assert member["slug"] == "jamie"
    assert member["name"] == "Jamie"
    assert member["is_admin"] is True


def test_member_scope_is_not_admin_by_default():
    token = webapp.mint_token(_jamie_id(), is_admin=False)
    assert webapp.resolve_token(token)["is_admin"] is False


def test_unknown_and_empty_token_resolve_to_none():
    assert webapp.resolve_token("nope-not-a-real-token") is None
    assert webapp.resolve_token(None) is None
    assert webapp.resolve_token("") is None


def test_expired_token_resolves_to_none():
    token = webapp.mint_token(_jamie_id(), is_admin=False, ttl=timedelta(seconds=-1))
    assert webapp.resolve_token(token) is None
