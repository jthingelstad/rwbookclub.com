"""Seed the Book Cloud from the mailing-list archive (2016→) — one deliberate run.

Replays member-authored archive mail per member per year and asks Sonnet to EXTRACT genuine book
references — title + WHY it came up (the Ethnographer rule: the reason is the cultural payload;
"mentioned in chat" is a failed reason; precision over recall). Each mention is inserted into
`book_cloud` BACKDATED to its message's real sent date, so first/last-mention aggregation is
historically true. Mentions of books the club has READ are skipped (the corpus and reviews own
those); the cloud is for the books we orbit but haven't read.

Boundary: only mail with sent_at <= the weekly reflection job's mail cursor (--until defaults to
it) — same convention as the memory mining.

    python -m agent.script.archive.mine_archive_book_cloud --member tom --year 2018 --dry-run
    python -m agent.script.archive.mine_archive_book_cloud             # full run
    python -m agent.script.archive.mine_archive_book_cloud --report    # aggregated cloud

Resumable: job_state['archive_mining_cloud'] records the highest completed year per member; a
lane stops at its first failed year and resumes there on re-run.
"""

from __future__ import annotations

import argparse
import json
import logging
import re

from agent import config, db, oliver, reflection
from agent import corpus_read as cr

log = logging.getLogger("oliver.book_cloud_mining")

JOB_KEY = "archive_mining_cloud"
FIRST_YEAR = 2016
MAX_BODY = 1200
_JSON_RE = re.compile(r"\[.*\]", re.S)

SYSTEM = (
    "You are Oliver, the R/W Book Club's assistant, building the club's private Book Cloud from "
    "its mailing-list archive: books a member genuinely referenced that the club has NOT read.\n\n"
    "You will get one member's emails from one year, each tagged [id | date | subject]. Extract "
    "every GENUINE book reference — a nomination, a comparison, a recommendation, an objection, a "
    "running joke — with WHY it came up.\n\n"
    "RULES:\n"
    "- The REASON is the point. One short, specific, club-readable sentence preserving the "
    "connection ('nominated as a systems-thinking pick', 'compared to X to argue Y'). A reason "
    "like 'mentioned in an email' is a failure — skip the mention instead.\n"
    "- PRECISION OVER RECALL: skip vague title-shaped phrases, articles/films/podcasts, and "
    "anything you can't confidently read as a book reference.\n"
    "- SKIP books on the club's read list (provided below) — the club record owns those. This "
    "cloud is only for books the club has NOT read.\n"
    "- Privacy: reasons must be club-operational/literary; never health, finances, "
    "relationships, or conflict between members.\n"
    "- reason_kind: one of nomination | comparison | objection | recommendation | side_reference "
    "| joke.\n"
    "- message_id: copy the [id] of the email the mention came from, exactly.\n\n"
    "OUTPUT: strict JSON only — a list (possibly empty), no prose, no code fences:\n"
    '[{"title": "…", "author": "…" or null, "reason": "…", "reason_kind": "…", '
    '"message_id": "…"}]'
)


def _read_titles() -> list[str]:
    return sorted({b.get("title") or "" for b in cr.books() if b.get("isRead")} - {""})


def _prompt(member: str, year: int, msgs: list[dict], read_titles: list[str]) -> str:
    parts = [f"Member: {member} — emails from {year}\n",
             "Books the club has READ (skip mentions of these):",
             "; ".join(read_titles), "\nEmails:"]
    for m in msgs:
        body = (m.get("body_clean") or "")[:MAX_BODY]
        parts.append(f"[{m['message_id']} | {(m.get('sent_at') or '')[:10]} | "
                     f"{m.get('subject') or '(no subject)'}]\n{body}\n")
    return "\n".join(parts)


def _parse(raw: str) -> list | None:
    m = _JSON_RE.search(raw or "")
    if not m:
        return [] if (raw or "").strip() in ("", "[]") else None
    try:
        out = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    return out if isinstance(out, list) else None


def _mine_member_year(member: str, year: int, *, until: str, dry_run: bool) -> dict | None:
    """Extract one member-year. Returns counts, or None on a (retried) parse failure."""
    start, end = f"{year}-01-01", min(f"{year + 1}-01-01", until)
    msgs = db.mail_messages_between(start, end, member_slug=member,
                                    exclude_from=config.OLIVER_EMAIL_ADDRESS)
    if not msgs:
        return {"mentions": 0, "messages": 0}
    by_id = {m["message_id"]: m for m in msgs}
    read_titles = _read_titles()
    read_norm = {t.lower().strip() for t in read_titles}

    mentions = None
    for attempt in (1, 2):  # same stochastic-format + retry posture as the memory miner
        raw = oliver.complete(SYSTEM, _prompt(member, year, msgs, read_titles),
                              model=oliver.MODEL, thinking=False, effort=None, max_tokens=16000,
                              usage_channel=None if dry_run else "book_cloud:mining")
        mentions = _parse(raw)
        if mentions is not None:
            break
        log.warning("cloud mining: unparseable output for %s %s (attempt %d): %r",
                    member, year, attempt, (raw or "")[:300])
    if mentions is None:
        return None

    kept = 0
    for m in mentions:
        title = str(m.get("title") or "").strip()
        reason = str(m.get("reason") or "").strip()
        if not title or not reason or title.lower().strip() in read_norm:
            continue  # enforce the skip-read rule in code too
        src = by_id.get(str(m.get("message_id") or ""))
        if dry_run:
            print(f"  + {title}"
                  + (f" — {m.get('author')}" if m.get("author") else "")
                  + f" [{m.get('reason_kind') or '?'}] {reason}"
                  + (f"  ({(src or {}).get('sent_at', '')[:10]})" if src else ""))
        else:
            db.add_book_cloud_entry(
                title=title, reason=reason, author=(m.get("author") or None),
                reason_kind=(m.get("reason_kind") or None),
                book_slug=(cr.find_book(title) or {}).get("slug"),
                mentioned_by=member, surface="mailing_list",
                source_message_id=(src or {}).get("message_id"),
                created_at=((src or {}).get("sent_at") or None),
            )
        kept += 1
    return {"mentions": kept, "messages": len(msgs)}


def _report() -> None:
    agg = db.book_cloud_titles(limit=200)
    read_slugs = {b["slug"] for b in cr.books() if b.get("isRead")}
    unread = [r for r in agg if not (r.get("book_slug") and r["book_slug"] in read_slugs)]
    print(f"\n===== BOOK CLOUD ===== ({len(unread)} unread titles aggregated; showing by recency)")
    for r in unread[:60]:
        who = ",".join(r["mentioners"]) or "?"
        print(f"  {r['first_mentioned'][:10]}→{r['last_mentioned'][:10]} ×{r['mention_count']} "
              f"[{who}] {r['title']} — {r['recentReasons'][0] if r['recentReasons'] else ''}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--member")
    ap.add_argument("--year", type=int)
    ap.add_argument("--until")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    if args.report:
        _report()
        return

    until = args.until or (db.get_job_state(reflection.JOB_KEY) or {}).get("mail_sent_at")
    if not until:
        raise SystemExit("No boundary: run the weekly reflection once or pass --until.")
    years = [args.year] if args.year else list(range(FIRST_YEAR, int(until[:4]) + 1))
    members = [args.member] if args.member else sorted(
        m["slug"] for m in cr.members() if m.get("isCurrent"))

    for member in members:
        state = db.get_job_state(JOB_KEY) or {"done": {}}
        done_through = int((state.get("done") or {}).get(member, 0))
        total = 0
        print(f"\n### {member}")
        for year in years:
            if year <= done_through and not args.year:
                continue
            res = _mine_member_year(member, year, until=until, dry_run=args.dry_run)
            if res is None:
                print(f"  !! {member} {year}: unparseable — lane stopped; re-run to resume")
                break
            total += res["mentions"]
            if not args.dry_run and not args.year:
                state = db.get_job_state(JOB_KEY) or {"done": {}}
                state.setdefault("done", {})[member] = year
                state["until"] = until
                db.set_job_state(JOB_KEY, state)
        print(f"    mentions kept: {total}")

    if not args.dry_run:
        db.add_activity("reflection", "Book Cloud archive seeding run",
                        f"Members: {', '.join(members)}\nYears: {years[0]}–{years[-1]}")
        _report()


if __name__ == "__main__":
    main()
