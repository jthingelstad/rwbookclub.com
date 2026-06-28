"""Web-app spike: token mint/resolve (expiry, unknown, member join) + on-demand lifecycle."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import aiohttp

from agent import clubdb, config, webapp


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


def test_server_starts_on_demand_and_stops_when_idle(monkeypatch):
    """ensure_running() binds the server; with a zero idle timeout the watcher tears it back down,
    so nothing is left listening — the 'no access when unused' guarantee."""
    monkeypatch.setattr(config, "WEBAPP_PORT", 8799)
    monkeypatch.setattr(webapp, "_IDLE_TIMEOUT", timedelta(seconds=0))
    monkeypatch.setattr(webapp, "_CHECK_INTERVAL", 0.05)

    async def scenario():
        try:
            await webapp.ensure_running()
            assert webapp._runner is not None
            async with aiohttp.ClientSession() as s:
                async with s.get("http://127.0.0.1:8799/healthz") as r:
                    assert r.status == 200
            # idle timeout is 0 → the watcher should stop the server within a few ticks
            for _ in range(40):
                await asyncio.sleep(0.05)
                if webapp._runner is None:
                    break
            assert webapp._runner is None, "server should idle-shutdown when unused"
        finally:
            async with webapp._lock:
                if webapp._runner is not None:
                    await webapp._do_stop()

    asyncio.run(scenario())
