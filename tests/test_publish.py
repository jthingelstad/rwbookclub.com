"""The local build+deploy module and the coalescing background publisher.

The real gh-pages push is out of scope for CI; we cover the pure logic: the empty-site
guards (the live-site safety mechanism), binary resolution, and the publisher's
coalescing/failure behavior (so the last write of a burst is always deployed and a failed
deploy surfaces)."""

from __future__ import annotations

import asyncio

import pytest

from agent import commands, publish

# Captured before conftest's autouse `_no_publish` fixture stubs the module attr, so the
# guard tests can exercise the real function (its internals — ensure_corpus/_run/SITE_DIR —
# are still monkeypatched per-test, so no real build/deploy happens).
_REAL_PUBLISH_SITE = publish.publish_site


# ── publish_site guards + _bin ───────────────────────────────────────────────
def test_bin_resolves_real_and_raises_on_missing():
    assert publish._bin("git").endswith("git")
    with pytest.raises(publish.PublishError):
        publish._bin("definitely-not-a-real-binary-xyz")


def test_bin_falls_back_to_extra_dirs(monkeypatch):
    monkeypatch.setattr(publish.shutil, "which", lambda _name: None)
    # git lives in one of _EXTRA_BIN_DIRS (/usr/bin) → still resolved without PATH.
    assert publish._bin("git").endswith("/git")


def test_publish_site_refuses_empty_build(monkeypatch, tmp_path):
    monkeypatch.setattr(publish, "ensure_corpus", lambda: {})
    monkeypatch.setattr(publish, "_run", lambda *a, **k: None)        # fake npm build
    monkeypatch.setattr(publish, "SITE_DIR", tmp_path)               # empty → no index.html
    with pytest.raises(publish.PublishError, match="index.html"):
        _REAL_PUBLISH_SITE()


def test_publish_site_refuses_when_cname_missing(monkeypatch, tmp_path):
    (tmp_path / "index.html").write_text("<html></html>")            # built, but no CNAME
    monkeypatch.setattr(publish, "ensure_corpus", lambda: {})
    monkeypatch.setattr(publish, "_run", lambda *a, **k: None)
    monkeypatch.setattr(publish, "_deploy_gh_pages", lambda *a, **k: None)
    monkeypatch.setattr(publish, "SITE_DIR", tmp_path)
    with pytest.raises(publish.PublishError, match="CNAME"):
        _REAL_PUBLISH_SITE()


def test_publish_site_deploys_when_complete(monkeypatch, tmp_path):
    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "CNAME").write_text("rwbookclub.com")
    deployed = []
    monkeypatch.setattr(publish, "ensure_corpus", lambda: {"books": 1})
    monkeypatch.setattr(publish, "_run", lambda *a, **k: None)
    monkeypatch.setattr(publish, "_deploy_gh_pages", lambda msg: deployed.append(msg))
    monkeypatch.setattr(publish, "SITE_DIR", tmp_path)
    out = _REAL_PUBLISH_SITE()
    assert out["deployed"] is True and deployed == ["Deploy site"]


# ── coalescing background publisher ──────────────────────────────────────────
def _reset_publisher(monkeypatch):
    monkeypatch.setattr(commands, "_publisher_task", None)
    monkeypatch.setattr(commands, "_publish_dirty", False)


def test_publisher_reruns_for_a_write_that_lands_mid_build(monkeypatch):
    """The last write of a burst must be deployed: if a write marks the site dirty while a
    publish is in flight, the drain loop runs publish again."""
    _reset_publisher(monkeypatch)
    calls = []

    def fake_publish():
        calls.append(1)
        if len(calls) == 1:               # simulate a write landing during the first build
            commands._publish_dirty = True
        return {"deployed": True}

    monkeypatch.setattr(publish, "publish_site", fake_publish)

    async def run():
        commands.schedule_publish()
        await commands._publisher_task

    asyncio.run(run())
    assert len(calls) == 2  # re-ran to capture the mid-build write


def test_publisher_is_strongly_referenced(monkeypatch):
    """The task must be held in a module global (un-referenced tasks can be GC'd mid-run)."""
    _reset_publisher(monkeypatch)
    monkeypatch.setattr(publish, "publish_site", lambda: {"deployed": True})

    async def run():
        commands.schedule_publish()
        assert commands._publisher_task is not None
        await commands._publisher_task

    asyncio.run(run())


def test_publisher_surfaces_failure_as_warning(monkeypatch):
    _reset_publisher(monkeypatch)

    def boom():
        raise RuntimeError("build failed")

    activities = []
    monkeypatch.setattr(publish, "publish_site", boom)
    monkeypatch.setattr(commands.db, "add_activity", lambda *a, **k: activities.append(a))

    async def run():
        commands.schedule_publish()
        await commands._publisher_task

    asyncio.run(run())
    assert any(a[0] == "warning" for a in activities)
