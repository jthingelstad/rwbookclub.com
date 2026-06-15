"""Tinylytics integration for Oliver email open tracking."""

from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import quote, urlencode

import requests

from agent import config, db

TRACKING_PATH_PREFIX = "/oliver/email/"


def enabled() -> bool:
    return bool(
        config.TINYLYTICS_SITE_ID
        and config.TINYLYTICS_SITE_ID_NUMERIC
        and config.TINYLYTICS_API_KEY
    )


def tracking_path(token: str) -> str:
    return f"{TRACKING_PATH_PREFIX}{token}"


def pixel_url(token: str) -> str:
    if not config.TINYLYTICS_SITE_ID:
        raise RuntimeError("TINYLYTICS_SITE_ID is not configured")
    params = urlencode({"path": tracking_path(token)})
    site = quote(config.TINYLYTICS_SITE_ID, safe="")
    return f"{config.TINYLYTICS_PIXEL_BASE_URL}/{site}.gif?{params}"


def hits_for_path(path: str, *, start_date: date | None = None,
                  end_date: date | None = None) -> int:
    if not enabled():
        return 0
    start = start_date or (date.today() - timedelta(days=30))
    end = end_date or date.today()
    r = requests.get(
        f"{config.TINYLYTICS_API_BASE_URL}/sites/{config.TINYLYTICS_SITE_ID_NUMERIC}/hits",
        params={
            "path": path,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "time_zone": "user",
            "per_page": 1,
        },
        headers={
            "Authorization": f"Bearer {config.TINYLYTICS_API_KEY}",
            "Accept": "application/json",
        },
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    pagination = data.get("pagination") or {}
    return int(pagination.get("total_count") or len(data.get("hits") or []))


def sync_email_opens(*, limit: int = 100) -> int:
    """Poll Tinylytics for tracked email paths and mark newly observed opens."""
    if not enabled():
        return 0
    synced = 0
    for row in db.tracked_emails_without_open(limit=limit):
        token = row["token"]
        if hits_for_path(tracking_path(token)) <= 0:
            continue
        db.record_email_open(
            token,
            remote_addr="tinylytics",
            user_agent="tinylytics-api",
        )
        synced += 1
    return synced
