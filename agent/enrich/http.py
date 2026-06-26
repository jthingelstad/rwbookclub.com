"""One polite, shared HTTP client for all enrichment sources.

A single ``requests.Session`` with a real User-Agent (per OL/Wikimedia etiquette),
a short inter-request sleep, timeouts, and best-effort error handling: any network
or parse failure returns ``None`` so a blip just leaves a gap for the next pass
rather than crashing the loop.
"""

from __future__ import annotations

import time

import requests

USER_AGENT = (
    "rwbookclub-enricher/1.0 (https://rwbookclub.com; book-club data enrichment)"
)
SLEEP_BETWEEN = 0.2  # polite pause between requests
DEFAULT_TIMEOUT = 20

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})


def get_json(url: str, params: dict | None = None, timeout: int = DEFAULT_TIMEOUT) -> dict | list | None:
    """GET and parse JSON, returning None on any error (non-fatal by design)."""
    try:
        r = SESSION.get(url, params=params, timeout=timeout)
        ok = r.ok
        data = r.json() if ok else None
    except Exception:  # noqa: BLE001 - network/parse errors are non-fatal
        data = None
    time.sleep(SLEEP_BETWEEN)
    return data


def get_bytes(url: str, timeout: int = 60) -> bytes | None:
    """GET raw bytes (images), returning None on any error."""
    try:
        r = SESSION.get(url, timeout=timeout)
        content = r.content if r.ok else None
    except Exception:  # noqa: BLE001
        content = None
    time.sleep(SLEEP_BETWEEN)
    return content
