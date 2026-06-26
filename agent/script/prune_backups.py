"""Prune Oliver's local DB migration backups.

The ``agent/backups/`` snapshots are one-off pre-migration safety copies (gitignored, ~19 MB
each). Nothing rotates them, so they accrue one per migration. This keeps the KEEP_RECENT
most-recent snapshots uncompressed (handy for a quick restore) and gzips the rest in place
(reversible, ~4× smaller). Run manually after a migration::

    python -m agent.script.prune_backups
"""

from __future__ import annotations

import gzip
import shutil
from pathlib import Path

BACKUPS = Path(__file__).resolve().parents[1] / "backups"
KEEP_RECENT = 2


def main() -> None:
    dbs = sorted(BACKUPS.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    keep, to_gzip = dbs[:KEEP_RECENT], dbs[KEEP_RECENT:]
    for p in to_gzip:
        gz = p.with_suffix(p.suffix + ".gz")
        with open(p, "rb") as src, gzip.open(gz, "wb") as dst:
            shutil.copyfileobj(src, dst)
        p.unlink()
        print(f"gzipped {p.name} -> {gz.name}")
    print(f"kept {len(keep)} recent uncompressed, gzipped {len(to_gzip)}")


if __name__ == "__main__":
    main()
