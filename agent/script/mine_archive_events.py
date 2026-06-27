"""Mine the email archive into Oliver's club timeline (Phase 2 of the event system).

Run deliberately, like `agent/enrich/` — it calls Opus once per mail thread, so it costs real
money and takes a while. It NEVER writes to the `events` table directly: it walks the archive
thread by thread (oldest-first), asks Opus to extract zero or more durable club events per
thread, and appends them as *candidates* to a review file. A human reviews that file, flips
`"approve": true` on the keepers, and a separate `--load` pass inserts only the approved,
not-yet-inserted candidates. Provenance (`source = "mail:<thread_id>#<n>"`) makes every mined
event auditable and removable, and makes the loader idempotent.

Usage (from the repo root, with ANTHROPIC_API_KEY in the env / shared .env):

    # Mine — append candidates to agent/logs/mined_events.jsonl (resumable; skips done threads):
    python -m agent.script.mine_archive_events --limit 20        # first 20 threads (smoke test)
    python -m agent.script.mine_archive_events                   # the whole archive
    python -m agent.script.mine_archive_events --thread <id>     # one specific thread
    python -m agent.script.mine_archive_events --force           # re-mine threads already done

    # Review: open agent/logs/mined_events.jsonl, set "approve": true on the events to keep
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
        "group (a new job, a move, a new child, or a planned vacation/travel that affects their "
        "attendance — use member_away for the latter). Do NOT record anything sensitive: "
        "health/medical, finances, relationship or family trouble, job loss, or anything a member "
        "would not want preserved in a shared club history. When in doubt, leave it out.\n"
        "- Prefer fewer, higher-signal events; a typical thread yields 0–3. A scheduling thread that "
        "settled a meeting date yields one meeting_scheduled (or meeting_rescheduled); if nothing "
        "durable came of it, return [].\n"
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


def mine(*, limit: int | None, force: bool, thread_id: str | None,
         out_path: pathlib.Path, model: str, effort: str) -> dict:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    progress_path = out_path.with_suffix(out_path.suffix + ".threads")

    roster = _roster()
    slugs = {m["slug"] for m in roster}
    system = _system_prompt(roster)

    if thread_id:
        threads = [{"thread_id": thread_id}]
    else:
        threads = db.list_mail_threads()
    done = set() if force else _done_threads(progress_path)
    todo = [t for t in threads if t["thread_id"] not in done]
    if limit is not None:
        todo = todo[:limit]

    print(f"mining {len(todo)} thread(s) "
          f"(skipping {len(threads) - len(todo)} already done / over-limit); model={model} effort={effort}")
    stats = {"threads": 0, "events": 0, "empty": 0, "errors": 0}
    for i, t in enumerate(todo, 1):
        tid = t["thread_id"]
        full = db.get_mail_thread(tid, limit=MAX_MESSAGES_PER_THREAD)
        if not full or not full.get("messages"):
            print(f"  [{i}/{len(todo)}] {tid}: no messages, skipping")
            _append_line(progress_path, tid)
            continue
        try:
            reply = oliver.complete(system, _thread_prompt(full), model=model, effort=effort)
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


def _append_line(path: pathlib.Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(text + "\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Mine the email archive into Oliver's club timeline.")
    p.add_argument("--load", action="store_true",
                   help="Insert approved candidates from the review file (instead of mining).")
    p.add_argument("--limit", type=int, default=None, help="Max threads to mine this run.")
    p.add_argument("--thread", default=None, help="Mine a single thread id.")
    p.add_argument("--force", action="store_true", help="Re-mine threads already marked done.")
    p.add_argument("--out", type=pathlib.Path, default=OUT_PATH, help="Review file path.")
    p.add_argument("--model", default=oliver.OPUS_MODEL, help="Model id for extraction.")
    p.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"],
                   help="Reasoning effort for extraction.")
    args = p.parse_args(argv)

    if args.load:
        load(out_path=args.out)
        return 0
    mine(limit=args.limit, force=args.force, thread_id=args.thread,
         out_path=args.out, model=args.model, effort=args.effort)
    return 0


if __name__ == "__main__":
    sys.exit(main())
