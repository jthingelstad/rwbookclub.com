"""Mine the mailing-list archive (2016→) into Oliver's reflective memory — one deliberate run.

Replays the archive CHRONOLOGICALLY through the reflection consolidator (agent/reflection.py):
per member, year by year, each step feeds that year's messages plus the member's current memory
set into the same add/update/retire contract — so later evidence updates or retires earlier
notes, ending with a current-but-history-aware set. A second lane does the same per year with
ALL members' messages for CLUB lore (traditions, running jokes, recurring debates; privacy
boundary in the club prompt). Mined memories carry source='reflection', so the weekly reflection
job owns and grooms them forever after.

Boundary: only mail with sent_at <= the weekly reflection job's initial mail cursor is mined
(--until defaults to it), so mining and the weekly job never overlap.

Run deliberately, like agent/enrich/ — ~66 Sonnet calls over the full archive:

    python -m agent.script.archive.mine_archive_memories --member tom --dry-run
    python -m agent.script.archive.mine_archive_memories --club-only --year 2018 --dry-run
    python -m agent.script.archive.mine_archive_memories                  # the whole archive
    python -m agent.script.archive.mine_archive_memories --report         # just print final sets

Resumable: job_state['archive_mining'] records the highest completed year per lane; a re-run
skips completed years. A lane stops at the first failed year (so chronology and the resume
cursor stay consistent) and picks up there next run.
"""

from __future__ import annotations

import argparse

from agent import config, db, reflection
from agent import corpus_read as cr

JOB_KEY = "archive_mining"
FIRST_YEAR = 2016  # the archive starts 2016-08
CLUB_LANE = "_club"


def _members() -> list[str]:
    return sorted(m["slug"] for m in cr.members() if m.get("isCurrent"))


def _lines(msgs: list[dict]) -> list[str]:
    return [f"[mailing list] {m.get('member_slug')} — {m.get('subject') or '(no subject)'}: "
            f"{(m.get('body_clean') or '')[:1500]}" for m in msgs]


def _era_note(year: int) -> str:
    return (f"This material is all from {year}. Prefer durable tastes and lore over moment-to-"
            f"moment chatter; if an opinion or event is clearly time-bound, note the year in "
            f"the memory.")


def _mine_lane(lane: str, *, until: str, years: list[int], dry_run: bool) -> dict:
    """Replay one lane (a member slug, or CLUB_LANE) through its years. Stops at the first
    failure so chronology and the resume cursor stay consistent."""
    state = db.get_job_state(JOB_KEY) or {"done": {}}
    done_through = int((state.get("done") or {}).get(lane, 0))
    counts = {"add": 0, "update": 0, "retire": 0, "years": 0}
    for year in years:
        if year <= done_through:
            continue
        start, end = f"{year}-01-01", min(f"{year + 1}-01-01", until)
        member = None if lane == CLUB_LANE else lane
        msgs = db.mail_messages_between(start, end, member_slug=member,
                                        exclude_from=config.OLIVER_EMAIL_ADDRESS)
        if msgs:
            scope = "club" if lane == CLUB_LANE else "member"
            res = reflection.consolidate(
                _lines(msgs), scope=scope, subject=member, era_note=_era_note(year),
                dry_run=dry_run, usage_channel="reflection:mining")
            if "skipped" in res:
                print(f"  !! {lane} {year}: {res['skipped']} — lane stopped; re-run to resume here")
                break
            for k in ("add", "update", "retire"):
                counts[k] += res[k]
            counts["years"] += 1
        if not dry_run:  # advance even through empty years so resume skips them
            state = db.get_job_state(JOB_KEY) or {"done": {}}
            state.setdefault("done", {})[lane] = year
            state["until"] = until
            db.set_job_state(JOB_KEY, state)
    return counts


def _report() -> None:
    print("\n===== FINAL MEMORY SETS (source=reflection) =====")
    for slug in _members():
        mems = [m for m in db.get_memories(subject=slug) if m["source"] == reflection.SOURCE]
        print(f"\n== {slug} ({len(mems)}) ==")
        for m in mems:
            print(f"  [{m['id']}] {m['note']}")
    club = [m for m in db.get_memories(scope="club") if m["source"] == reflection.SOURCE]
    print(f"\n== club lore ({len(club)}) ==")
    for m in club:
        print(f"  [{m['id']}] {m['note']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="preview proposals; write nothing")
    ap.add_argument("--member", help="mine only this member slug")
    ap.add_argument("--club-only", action="store_true", help="mine only the club-lore lane")
    ap.add_argument("--members-only", action="store_true", help="skip the club-lore lane")
    ap.add_argument("--year", type=int, help="mine only this single year (calibration)")
    ap.add_argument("--until", help="mine mail with sent_at <= this ISO instant "
                                    "(default: the weekly reflection job's initial mail cursor)")
    ap.add_argument("--report", action="store_true", help="only print the final memory sets")
    args = ap.parse_args()

    if args.report:
        _report()
        return

    until = args.until or (db.get_job_state(reflection.JOB_KEY) or {}).get("mail_sent_at")
    if not until:
        raise SystemExit("No boundary: the weekly reflection job has no mail cursor yet and no "
                         "--until was given. Run the weekly job once (or pass --until).")
    last_year = int(until[:4])
    years = [args.year] if args.year else list(range(FIRST_YEAR, last_year + 1))

    lanes: list[str] = []
    if not args.club_only:
        lanes += [args.member] if args.member else _members()
    if not args.members_only and not args.member:
        lanes.append(CLUB_LANE)

    for lane in lanes:
        print(f"\n### lane: {lane}")
        counts = _mine_lane(lane, until=until, years=years, dry_run=args.dry_run)
        print(f"    {counts}")

    if not args.dry_run:
        db.add_activity("reflection", "Archive memory mining run",
                        f"Lanes: {', '.join(lanes)}\nYears: {years[0]}–{years[-1]}\nUntil: {until}")
        _report()


if __name__ == "__main__":
    main()
