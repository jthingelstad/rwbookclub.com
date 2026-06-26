"""Dump the public-safe ``club_*`` tables to a SQL fixture for tests / CI.

The corpus is no longer committed to git, so the test suite can't seed ``club_*`` from
committed corpus files. Instead it seeds from this fixture. The ``club_*`` tables carry
**no PII** (emails / mailing list / mail archive live in ``member_identities`` / ``mail_*``,
which are NOT dumped here), so the fixture is safe to commit.

Data-only INSERTs (the schema is created by ``clubdb.ensure_schema()`` in conftest), emitted
in ``CLUB_TABLES`` order (parents before children) so a straight replay is FK-safe.

Regenerate after a club_* schema or seed-data change::

    python -m agent.script.dump_club_seed > tests/fixtures/club_seed.sql
"""

from __future__ import annotations

import sys

from agent import clubdb, db


def _lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def main() -> None:
    out = sys.stdout
    out.write("-- Public-safe club_* seed for tests/CI (NO PII). Regenerate with:\n")
    out.write("--   python -m agent.script.dump_club_seed > tests/fixtures/club_seed.sql\n")
    with db.connect() as conn:
        for table in clubdb.CLUB_TABLES:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                continue
            cols = rows[0].keys()
            collist = ", ".join(cols)
            out.write(f"\n-- {table} ({len(rows)} rows)\n")
            for row in rows:
                vals = ", ".join(_lit(row[c]) for c in cols)
                out.write(f"INSERT INTO {table} ({collist}) VALUES ({vals});\n")


if __name__ == "__main__":
    main()
