"""Airtable cold-backup client — only used by fetch.py / migrate.py / restore_*.

Git is the canonical source of truth for the corpus; this module exists so
the rare cold-backup re-pull path keeps working. Paths and the slug helper
live in `corpus.paths` — import those from there, not here.
"""

from __future__ import annotations

import os
import time

import requests
from dotenv import load_dotenv

from corpus.paths import REPO_ROOT

# Re-export so existing `from corpus.airtable import DATA_DIR` style imports
# keep working during the migration. Prefer importing from corpus.paths for
# new code; this re-export will be retired once all callers are migrated.
from corpus.paths import (  # noqa: F401
    CORPUS_DIR, DATA_DIR, RAW_DATA_DIR, COVERS_DIR, MEMBERS_IMG_DIR, slugify,
)

# Airtable table IDs are stable; names can change. Pulled from CLAUDE.md.
BOOKS = "tblPqH96wIgGuUSXe"
MEETINGS = "tblJpQrukeCXaO0Uq"
MEMBERS = "tblsjVRbdj231zbwj"
AUTHORS = "tblLkEUVXxLMynFtn"
REVIEWS = "tblxZR21gPDPYBfA1"
AWARDS = "tblrIaGgMtA08xyJE"


def load_env() -> tuple[str, str]:
    """Load Airtable credentials from .env or process env."""
    load_dotenv(REPO_ROOT / ".env")
    base = os.environ.get("AIRTABLE_BASE_ID")
    pat = os.environ.get("AIRTABLE_PAT")
    if not base or not pat:
        raise SystemExit(
            "AIRTABLE_BASE_ID and AIRTABLE_PAT must be set (in .env locally or as GH secrets in CI)"
        )
    return base, pat


def airtable_session(pat: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {pat}"})
    return s


def list_all(session: requests.Session, base: str, table_id: str) -> list[dict]:
    """Page through every record in a table."""
    out: list[dict] = []
    offset: str | None = None
    while True:
        params: dict[str, str | int] = {"pageSize": 100}
        if offset:
            params["offset"] = offset
        r = session.get(
            f"https://api.airtable.com/v0/{base}/{table_id}",
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        d = r.json()
        out.extend(d.get("records", []))
        offset = d.get("offset")
        if not offset:
            return out
        time.sleep(0.2)  # gentle rate limit


def first_attachment_url(field) -> str | None:
    if not field or not isinstance(field, list):
        return None
    return field[0].get("url")
