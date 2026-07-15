"""Generate a public-safe corpus for clean-room website builds.

The live corpus is private and gitignored. CI instead points Oliver at a scratch
database, loads the PII-free SQL fixture, and generates the same on-disk shape
that Eleventy consumes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    if not os.environ.get("OLIVER_DB_PATH") or not os.environ.get("OLIVER_CORPUS_DIR"):
        raise SystemExit("OLIVER_DB_PATH and OLIVER_CORPUS_DIR must both point to scratch paths")

    # These imports must follow the environment guard: db.DB_PATH and
    # corpus.paths.DATA_DIR are resolved at import time.
    from agent import corpus_gen, database, db

    fixture = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "club_seed.sql"
    database.initialize()
    with db.connect() as conn:
        conn.executescript(fixture.read_text())
    written = corpus_gen.generate()
    print(f"Generated public fixture corpus: {written}")


if __name__ == "__main__":
    sys.exit(main())
