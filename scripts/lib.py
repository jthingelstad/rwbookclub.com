"""Shared utilities for the Airtable → 11ty data pipeline."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from unidecode import unidecode

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "src" / "_data"
RAW_DATA_DIR = DATA_DIR / "raw"
COVERS_DIR = ROOT / "src" / "assets" / "images" / "covers"
MEMBERS_IMG_DIR = ROOT / "src" / "assets" / "images" / "members"

# Table IDs are stable; names can change. Pulled from CLAUDE.md.
BOOKS = "tblPqH96wIgGuUSXe"
MEETINGS = "tblJpQrukeCXaO0Uq"
MEMBERS = "tblsjVRbdj231zbwj"
AUTHORS = "tblLkEUVXxLMynFtn"
REVIEWS = "tblxZR21gPDPYBfA1"
AWARDS = "tblrIaGgMtA08xyJE"


def load_env() -> tuple[str, str]:
    """Load Airtable credentials from .env or process env."""
    load_dotenv(ROOT / ".env")
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


_slug_re = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """URL-safe slug. Transliterates non-ASCII (Cræft → craeft, Freedom™ → freedom)."""
    if not text:
        return ""
    ascii_text = unidecode(text).lower()
    ascii_text = _slug_re.sub("-", ascii_text).strip("-")
    return ascii_text


def first_attachment_url(field) -> str | None:
    if not field or not isinstance(field, list):
        return None
    return field[0].get("url")
