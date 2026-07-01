"""Mine the email archive into Oliver's club timeline (Phase 2 of the event system).

Run deliberately, like `agent/enrich/` — it calls Opus once per mail thread, so it costs real
money and takes a while. It NEVER writes to the `events` table directly: it walks the archive
thread by thread (oldest-first), asks Opus to extract zero or more durable club events per
thread, and appends them as *candidates* to a review file. A human reviews that file, flips
`"approve": true` on the keepers, and a separate `--load` pass inserts only the approved,
not-yet-inserted candidates. Provenance (`source = "mail:<thread_id>#<n>"`) makes every mined
event auditable and removable, and makes the loader idempotent.

Extraction defaults to Sonnet (constrained extraction — Opus is overkill); override with --model.

Usage (from the repo root, with ANTHROPIC_API_KEY in the env / shared .env):

    # Mine — append candidates to agent/logs/mined_events.jsonl (resumable; skips done threads):
    python -m agent.script.mine_archive_events --sample 30 --out /tmp/calib.jsonl  # calibration sample
    python -m agent.script.mine_archive_events                   # the whole archive (Sonnet)
    python -m agent.script.mine_archive_events --model claude-haiku-4-5            # cheaper model
    python -m agent.script.mine_archive_events --thread <id>     # one specific thread
    python -m agent.script.mine_archive_events --force           # re-mine threads already done

    # Review: --stats summarizes counts + lists every privacy-sensitive (member_life/social) event;
    #         --approve bulk-sets approve:true on whole safe categories (sensitive ones refused).
    python -m agent.script.mine_archive_events --stats
    python -m agent.script.mine_archive_events --approve meeting,selection,reading,club
    #         then hand-edit the file: set "approve": true on the member_life/social keepers
    #         (delete or leave approve:false to reject). Fix dates/slugs/summaries as needed.

    # Load — insert approved candidates into the timeline (idempotent; safe to re-run):
    python -m agent.script.mine_archive_events --load

The privacy boundary baked into the prompt: club-operational events + clearly-shared celebratory
milestones only; never health, finances, relationships, conflict, or anything sensitive.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import date

from agent import clubdb, corpus_read, db, oliver

OUT_PATH = pathlib.Path(__file__).resolve().parents[2] / "agent" / "logs" / "mined_events.jsonl"

# Token control: archives have a few enormous scheduling threads. Cap how much of each thread we
# feed the model so one thread can't blow the budget; a typical thread is well under these limits.
MAX_MESSAGES_PER_THREAD = 25
MAX_BODY_CHARS = 1200

_ALLOWED = db.CHRONICLE_KINDS  # category -> allowed kinds


def _roster() -> list[dict]:
    """All club members (current and past — the chronicle spans the whole history), name + slug."""
    return sorted(
        ({"slug": m.get("slug"), "name": m.get("name"), "current": bool(m.get("isCurrent"))}
         for m in corpus_read.members() if m.get("slug")),
        key=lambda m: m["name"] or m["slug"],
    )


def _system_prompt(roster: list[dict]) -> str:
    taxonomy = "\n".join(
        f"- {cat}: {', '.join(kinds)}" for cat, kinds in _ALLOWED.items()
    )
    roster_lines = "\n".join(
        f"- {m['name']} → {m['slug']}" + ("" if m["current"] else " (former member)")
        for m in roster
    )
    return (
        "You are the archivist for the R/W Book Club, which has met roughly monthly since April "
        "2003. You are reading the club's email archive one thread at a time to build a factual "
        "TIMELINE of the club's history. For the thread below, extract the durable club events it "
        "documents.\n\n"
        "Output a STRICT JSON array and NOTHING else. Each element:\n"
        '{\n'
        '  "category": one of [meeting, selection, social, member_life, club, reading],\n'
        '  "kind": one of the kinds listed for that category,\n'
        '  "occurred_at": "YYYY-MM-DD" (the day it happened — use the email dates in the thread; '
        "if no single exact day is clear, use the date of the most relevant message),\n"
        '  "member_slugs": [slugs from the roster the event is about; [] if club-wide or about no '
        "one specific member],\n"
        '  "summary": one factual past-tense sentence, naming names and the book/topic\n'
        "}\n\n"
        f"Allowed category → kinds:\n{taxonomy}\n\n"
        f"Member roster (name → slug):\n{roster_lines}\n\n"
        "RULES:\n"
        "- Extract ONLY what the thread explicitly states or clearly implies. Never invent events, "
        "dates, people, or books. If the thread is just chatter with no durable event, return [].\n"
        "- Map people to roster slugs. If someone isn't in the roster, omit them — never guess a slug.\n"
        "- PRIVACY: record only club-operational events (meetings scheduled/held/moved, book "
        "nominations/polls/votes/picks, hosting, dinners and spouses events, members joining or "
        "leaving) and clearly-shared, celebratory personal MILESTONES a member announced to the "
        "group (a new job, a move, a new child, or planned travel that affects their attendance — "
        "use member_away for the latter). Do NOT record anything sensitive: health/medical "
        "(illness, COVID, surgery, a procedure, recovery), finances, relationship or family "
        "trouble, job loss, or anything a member would not want preserved in a shared club "
        "history. When in doubt, leave it out.\n"
        "- member_away is ONLY for a member proactively announcing they will miss UPCOMING "
        "meeting(s) or be away for a stretch — a forward-looking heads-up (\"Tom will be away "
        "Feb 8–28\"). Do NOT create a member_away for a routine same-day \"can't make it tonight\" "
        "no-show — that is low-signal and already implied by the meeting. When you do record one, "
        "give only the absence and dates — NEVER a medical/health or otherwise personal reason "
        "(include a reason only if plainly non-sensitive, e.g. travel, and keep it minimal).\n"
        "- A member's death or serious illness is sensitive — do not record it; leave such "
        "memorial/health threads for a human to handle.\n"
        "- A meeting_held summary describes the MEETING — its date, the book discussed, and the "
        "location/format. Do NOT list who attended or was absent, and leave its member_slugs empty "
        "([]); attendance is not a timeline event.\n"
        "- For one meeting, record EITHER meeting_scheduled (the thread only shows it being planned) "
        "OR meeting_held (the thread shows it happened) — never both for the same meeting; if it "
        "happened, record meeting_held.\n"
        "- Granularity is PER MEMBER PER ROUND, not per book. A pickers thread where four members "
        "each pick a book yields four book_picked events (one per member). But if ONE member "
        "nominates or picks several books in a single message, that is ONE event for that member "
        "listing those books — never one event per book.\n"
        "- These ARE durable even when the surrounding thread is mostly logistics — keep them: a "
        "member proactively announcing upcoming travel/absence (member_away), and a decision to drop "
        "or swap a book mid-read (dnf). Don't discard a whole thread as chatter if it contains one "
        "of these.\n"
        "- Otherwise skip pure logistics chatter (\"what time works?\", \"see you there\") that "
        "settles nothing durable; many threads correctly yield []. Don't pad — but do keep every "
        "distinct pick, nomination, meeting, and decision.\n"
        "- Output the JSON array only — no prose, no markdown fences."
    )


def _thread_prompt(thread: dict) -> str:
    t = thread["thread"]
    msgs = thread["messages"][:MAX_MESSAGES_PER_THREAD]
    omitted = len(thread["messages"]) - len(msgs)
    lines = [
        f"THREAD: {t.get('subject_normalized') or '(no subject)'}",
        f"Spanning {(t.get('first_sent_at') or '?')[:10]} … {(t.get('last_sent_at') or '?')[:10]} "
        f"({t.get('message_count')} messages).",
        "",
    ]
    for m in msgs:
        who = m.get("from_name") or m.get("from_email") or "unknown"
        when = (m.get("sent_at") or "")[:10]
        body = (m.get("body_clean") or "").strip()
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS] + " […]"
        lines.append(f"--- {who} ({when}) ---\n{body}\n")
    if omitted > 0:
        lines.append(f"[{omitted} further message(s) omitted for length.]")
    return "\n".join(lines)


def _parse_events(text: str) -> list[dict]:
    """Tolerantly pull the JSON array out of the model's reply (strip ```json fences, find the
    outermost [...]). Returns [] on anything unparseable rather than crashing the sweep."""
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    start, end = s.find("["), s.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        parsed = json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _normalize_date(value, fallback: str) -> str:
    raw = str(value or "")[:10]
    try:
        date.fromisoformat(raw)
        return raw
    except ValueError:
        return fallback


def _clean_event(raw: dict, *, thread: dict, slugs: set[str], index: int) -> dict | None:
    """Validate one model-emitted event against the taxonomy + roster; shape it into a review row.
    Returns None (dropped) if the kind isn't in the taxonomy."""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "").strip()
    category = next((c for c, kinds in _ALLOWED.items() if kind in kinds), None)
    if category is None:
        return None  # hallucinated/unknown kind — drop it
    t = thread["thread"]
    thread_id = t["thread_id"]
    member_slugs = [s for s in (raw.get("member_slugs") or []) if s in slugs]
    fallback_date = (t.get("first_sent_at") or "")[:10] or "2003-01-01"
    return {
        "approve": False,
        "source": f"mail:{thread_id}#{index}",
        "thread_id": thread_id,
        "thread_subject": t.get("subject_normalized"),
        "category": category,
        "kind": kind,
        "occurred_at": _normalize_date(raw.get("occurred_at"), fallback_date),
        "member_slugs": member_slugs,
        "summary": str(raw.get("summary") or "").strip(),
    }


def _done_threads(progress_path: pathlib.Path) -> set[str]:
    if not progress_path.exists():
        return set()
    return {line.strip() for line in progress_path.read_text().splitlines() if line.strip()}


def mine(*, limit: int | None, force: bool, thread_id: str | None, sample: int | None,
         out_path: pathlib.Path, model: str, thinking: bool = False) -> dict:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    progress_path = out_path.with_suffix(out_path.suffix + ".threads")

    roster = _roster()
    slugs = {m["slug"] for m in roster}
    system = _system_prompt(roster)

    if thread_id:
        threads = [{"thread_id": thread_id}]
    else:
        threads = db.list_mail_threads()
    if sample is not None and sample > 0 and len(threads) > sample:
        # Evenly-spaced sample across the whole (oldest-first) archive — spans years + thread
        # sizes, so a calibration run isn't biased to the earliest threads. Deterministic, so two
        # model runs at the same --sample compare the identical threads.
        stride = len(threads) // sample
        threads = threads[::stride][:sample]
    done = set() if force else _done_threads(progress_path)
    todo = [t for t in threads if t["thread_id"] not in done]
    if limit is not None:
        todo = todo[:limit]

    print(f"mining {len(todo)} thread(s) "
          f"(skipping {len(threads) - len(todo)} already done / over-limit); "
          f"model={model} thinking={thinking}")
    stats = {"threads": 0, "events": 0, "empty": 0, "errors": 0}
    for i, t in enumerate(todo, 1):
        tid = t["thread_id"]
        full = db.get_mail_thread(tid, limit=MAX_MESSAGES_PER_THREAD)
        if not full or not full.get("messages"):
            print(f"  [{i}/{len(todo)}] {tid}: no messages, skipping")
            _append_line(progress_path, tid)
            continue
        try:
            reply = oliver.complete(system, _thread_prompt(full), model=model, thinking=thinking)
        except Exception as e:  # noqa: BLE001 — one bad thread shouldn't abort a 543-thread run
            stats["errors"] += 1
            print(f"  [{i}/{len(todo)}] {tid}: ERROR {type(e).__name__}: {e} — leaving for a re-run")
            continue
        events = []
        for n, raw in enumerate(_parse_events(reply)):
            row = _clean_event(raw, thread=full, slugs=slugs, index=n)
            if row:
                events.append(row)
        for row in events:
            _append_line(out_path, json.dumps(row, ensure_ascii=False))
        _append_line(progress_path, tid)  # mark done only after candidates are durably written
        stats["threads"] += 1
        stats["events"] += len(events)
        if not events:
            stats["empty"] += 1
        subj = (full["thread"].get("subject_normalized") or "")[:48]
        print(f"  [{i}/{len(todo)}] {tid}: {len(events)} event(s)  {subj}")
    print(f"done: {stats['threads']} threads mined, {stats['events']} candidate event(s) "
          f"({stats['empty']} empty, {stats['errors']} errored). Review → {out_path}")
    return stats


def load(*, out_path: pathlib.Path) -> dict:
    if not out_path.exists():
        print(f"no review file at {out_path} — run the miner first.")
        return {"inserted": 0, "skipped": 0, "unapproved": 0, "invalid": 0}
    stats = {"inserted": 0, "skipped": 0, "unapproved": 0, "invalid": 0}
    for line in out_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            stats["invalid"] += 1
            continue
        if not row.get("approve"):
            stats["unapproved"] += 1
            continue
        source = row.get("source")
        kind = row.get("kind")
        if not source or kind not in db._KIND_CATEGORY:
            stats["invalid"] += 1
            continue
        if db.event_source_exists(source):
            stats["skipped"] += 1
            continue
        member_slugs = row.get("member_slugs") or []
        member_id = clubdb.lookup_member_id(member_slugs[0]) if len(member_slugs) == 1 else None
        db.record_event(
            actor="oliver",
            surface="system",
            kind=kind,
            category=row.get("category"),
            member_id=member_id,
            detail={"summary": row.get("summary"), "members": member_slugs},
            occurred_at=row.get("occurred_at"),
            source=source,
        )
        stats["inserted"] += 1
    print(f"loaded: {stats['inserted']} inserted, {stats['skipped']} already present, "
          f"{stats['unapproved']} not approved, {stats['invalid']} invalid.")
    return stats


def _read_rows(out_path: pathlib.Path) -> list[dict]:
    rows = []
    for line in out_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


# Categories that may carry personal information — always reviewed by hand, never bulk-approved.
SENSITIVE_CATEGORIES = {"member_life", "social"}


def stats(*, out_path: pathlib.Path) -> dict:
    """Summarize the review file so a few hundred candidates are scannable: counts by category,
    kind, and year, plus the full text of every privacy-sensitive (member_life/social) event."""
    if not out_path.exists():
        print(f"no review file at {out_path} — run the miner first.")
        return {}
    rows = _read_rows(out_path)
    by_cat: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    by_year: dict[str, int] = {}
    approved = 0
    for r in rows:
        by_cat[r.get("category")] = by_cat.get(r.get("category"), 0) + 1
        by_kind[r.get("kind")] = by_kind.get(r.get("kind"), 0) + 1
        by_year[(r.get("occurred_at") or "????")[:4]] = by_year.get((r.get("occurred_at") or "????")[:4], 0) + 1
        approved += bool(r.get("approve"))
    print(f"{len(rows)} candidate event(s) in {out_path}  ({approved} approved)\n")
    print("by category:")
    for c, n in sorted(by_cat.items(), key=lambda kv: -kv[1]):
        print(f"  {c:12} {n}")
    print("\nby kind:")
    for k, n in sorted(by_kind.items(), key=lambda kv: -kv[1]):
        print(f"  {k:22} {n}")
    print("\nby year:")
    for y, n in sorted(by_year.items()):
        print(f"  {y}  {n}")
    sensitive = [r for r in rows if r.get("category") in SENSITIVE_CATEGORIES]
    print(f"\n── {len(sensitive)} privacy-sensitive event(s) (member_life/social) — review each by hand ──")
    for r in sensitive:
        mark = "✓" if r.get("approve") else " "
        members = ", ".join(r.get("member_slugs") or []) or "—"
        print(f"  [{mark}] {(r.get('occurred_at') or '')[:10]} {r.get('kind'):16} ({members}) {r.get('summary')}")
    return {"total": len(rows), "approved": approved, "sensitive": len(sensitive)}


def approve(*, out_path: pathlib.Path, categories: set[str]) -> dict:
    """Bulk-set approve:true on every candidate in the named categories, then rewrite the file.
    Refuses the privacy-sensitive categories — those must be approved individually by hand-editing."""
    blocked = categories & SENSITIVE_CATEGORIES
    if blocked:
        print(f"refusing to bulk-approve {sorted(blocked)} — review member_life/social by hand "
              "(edit the file directly).")
        categories = categories - SENSITIVE_CATEGORIES
    if not categories:
        return {"approved": 0}
    rows = _read_rows(out_path)
    n = 0
    for r in rows:
        if r.get("category") in categories and not r.get("approve"):
            r["approve"] = True
            n += 1
    out_path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    print(f"approved {n} candidate(s) in categories {sorted(categories)}.")
    return {"approved": n}


def _append_line(path: pathlib.Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(text + "\n")


DEFAULT_MODEL = "claude-sonnet-5"  # mining is constrained extraction — no need for Opus


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Mine the email archive into Oliver's club timeline.")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--load", action="store_true",
                      help="Insert approved candidates from the review file (instead of mining).")
    mode.add_argument("--stats", action="store_true",
                      help="Summarize the review file (counts + sensitive events) without changing it.")
    mode.add_argument("--approve", metavar="CATS",
                      help="Bulk-approve candidates in these comma-separated categories "
                           "(member_life/social are refused — review those by hand).")
    p.add_argument("--limit", type=int, default=None, help="Max threads to mine this run.")
    p.add_argument("--sample", type=int, default=None,
                   help="Mine an evenly-spaced sample of N threads across the archive (for calibration).")
    p.add_argument("--thread", default=None, help="Mine a single thread id.")
    p.add_argument("--force", action="store_true", help="Re-mine threads already marked done.")
    p.add_argument("--out", type=pathlib.Path, default=OUT_PATH, help="Review file path.")
    p.add_argument("--model", default=DEFAULT_MODEL, help="Model id for extraction.")
    p.add_argument("--thinking", action="store_true",
                   help="Enable adaptive thinking (off by default — extraction is mechanical).")
    args = p.parse_args(argv)

    if args.load:
        load(out_path=args.out)
        return 0
    if args.stats:
        stats(out_path=args.out)
        return 0
    if args.approve:
        approve(out_path=args.out, categories={c.strip() for c in args.approve.split(",") if c.strip()})
        return 0
    mine(limit=args.limit, force=args.force, thread_id=args.thread, sample=args.sample,
         out_path=args.out, model=args.model, thinking=args.thinking)
    return 0


if __name__ == "__main__":
    sys.exit(main())
