"""Lifecycle service for coalesced site publishes and stale-site reconciliation.

The low-level build/deploy mechanics live in :mod:`agent.publish`. This module owns the
long-lived process state around them: coalescing bursts of writes into background publishes and
checking whether the deployed next-book marker still matches the authoritative corpus.
"""

from __future__ import annotations

import asyncio
import logging

import requests

from agent import config, corpus_read, db, publish

log = logging.getLogger("oliver.publishing")

_publisher_task: asyncio.Task | None = None
_publish_dirty = False
_NEXT_MARKER_URL = config.SITE_URL + "/next.json"


def schedule() -> None:
    """Mark the site dirty and ensure a background publisher is running."""
    global _publisher_task, _publish_dirty
    _publish_dirty = True
    if _publisher_task is not None and not _publisher_task.done():
        return
    _publisher_task = asyncio.create_task(_drain())


async def _drain() -> None:
    """Publish until every write observed during a build is represented on the site."""
    global _publish_dirty
    while _publish_dirty:
        _publish_dirty = False
        for _ in range(6):
            try:
                await asyncio.to_thread(publish.publish_site)
                break
            except publish.PublishBusy:
                await asyncio.sleep(20)
            except Exception:
                log.exception("background publish failed")
                db.add_activity(
                    "warning",
                    "Site publish failed",
                    "A data write succeeded but rebuilding/deploying the site failed. "
                    "Run `python -m agent.publish` manually, or check the logs.",
                )
                break


def _expected_next_book_slug() -> str | None:
    upcoming = [
        book for book in corpus_read.books() if book.get("isUpcoming") and book.get("meetingDate")
    ]
    if not upcoming:
        return None
    upcoming.sort(key=lambda book: book.get("meetingDate") or "")
    return upcoming[0].get("slug")


def _deployed_next_book_slug() -> tuple[bool, str | None]:
    """Return whether the marker is reachable and the deployed next-book slug."""
    try:
        response = requests.get(_NEXT_MARKER_URL, timeout=15)
    except requests.RequestException:
        return (False, None)
    if response.status_code == 404:
        return (True, None)
    if not response.ok:
        return (False, None)
    try:
        return (True, (response.json() or {}).get("nextBookSlug"))
    except ValueError:
        return (True, None)


async def reconcile() -> bool:
    """Publish when the deployed next-book marker is stale; otherwise do nothing."""
    expected = await asyncio.to_thread(_expected_next_book_slug)
    if expected is None:
        return False
    if _publish_dirty or (_publisher_task is not None and not _publisher_task.done()):
        return False
    reachable, deployed = await asyncio.to_thread(_deployed_next_book_slug)
    if not reachable:
        log.warning("site self-heal: couldn't read %s; skipping this cycle", _NEXT_MARKER_URL)
        return False
    if deployed == expected:
        return False
    log.info(
        "site self-heal: deployed next book %r != expected %r — publishing",
        deployed,
        expected,
    )
    db.add_activity(
        "site_selfheal",
        "Auto-publishing to fix a stale site",
        f"The live site's next book is {deployed or '(none — old build)'} but it should be "
        f"“{expected}”. Rebuilding and deploying so the site reflects the current meeting — "
        "no action needed.",
    )
    schedule()
    return True
