"""Filesystem paths + the slug helper — everything that's not Airtable-specific.

Imported by everything in `corpus/` and `agent/` that needs to resolve a file
in the corpus or normalize a name to a slug. The Airtable client + table ids
live in `corpus.airtable`; that module imports paths from here.
"""

from __future__ import annotations

import re
from pathlib import Path

from unidecode import unidecode

CORPUS_DIR = Path(__file__).resolve().parent
REPO_ROOT = CORPUS_DIR.parent
DATA_DIR = CORPUS_DIR / "data"
RAW_DATA_DIR = DATA_DIR / "raw"
# Responsive cover/photo variants are website presentation assets, so the
# image step writes them straight into the website tree.
COVERS_DIR = REPO_ROOT / "website" / "src" / "assets" / "images" / "covers"
MEMBERS_IMG_DIR = REPO_ROOT / "website" / "src" / "assets" / "images" / "members"


_slug_re = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """URL-safe slug. Transliterates non-ASCII (Cræft → craeft, Freedom™ → freedom)."""
    if not text:
        return ""
    ascii_text = unidecode(text).lower()
    ascii_text = _slug_re.sub("-", ascii_text).strip("-")
    return ascii_text
