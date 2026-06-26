"""Guard against silent drift of the hand-regenerated tests/fixtures/club_seed.sql.

The whole suite seeds club_* from this snapshot. If a club_* column is added/renamed/dropped
and the fixture isn't regenerated, an *added* column would load as NULL everywhere and the
suite would pass against unrealistic data. These assertions fail loudly with a regen hint
instead. (Regenerate: python -m agent.script.dump_club_seed > tests/fixtures/club_seed.sql)"""

from __future__ import annotations

import pathlib
import re

from agent import db

_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "club_seed.sql"


def _fixture_columns_by_table() -> dict[str, list[str]]:
    cols: dict[str, list[str]] = {}
    for m in re.finditer(r"INSERT INTO (\w+) \(([^)]+)\)", _FIXTURE.read_text()):
        cols.setdefault(m.group(1), [c.strip() for c in m.group(2).split(",")])
    return cols


def test_fixture_columns_match_live_schema():
    """Each table the fixture inserts into must list exactly the live schema's columns."""
    with db.connect() as conn:
        for table, fixture_cols in _fixture_columns_by_table().items():
            live = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            assert set(fixture_cols) == live, (
                f"{table}: fixture columns {sorted(fixture_cols)} != live schema "
                f"{sorted(live)} — regenerate tests/fixtures/club_seed.sql")


def test_fixture_seeds_core_tables():
    """The autouse seed must populate the core tables (catches a truncated/empty fixture)."""
    with db.connect() as conn:
        for table, floor in [("club_books", 100), ("club_authors", 100),
                             ("club_meetings", 100), ("club_members", 5)]:
            n = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            assert n >= floor, f"{table} has only {n} rows — fixture truncated?"


def test_fixture_has_no_cr_bytes():
    """The dump normalizes CR → LF so regen is byte-stable (git would strip CR otherwise)."""
    assert b"\r" not in _FIXTURE.read_bytes()
